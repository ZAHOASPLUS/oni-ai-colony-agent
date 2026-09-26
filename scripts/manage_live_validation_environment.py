#!/usr/bin/env python3
"""Manifest-bound prepare/finalize transaction for an ONI validation run.

The default is a read-only plan.  ``--apply`` is required for every write, and
live mode additionally requires an exact run-id confirmation.  Tests must pass
an isolated ``--test-root`` and may inject process state/faults only there.
"""

from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_outcomes import command_intent_hash
from agent.canonical_hash import CanonicalHashError, canonical_hash as contract_canonical_hash
from agent.command_queue import COMMAND_DIRS, COMMAND_PIPELINE_SCHEMA, command_pipeline_readiness
from agent.state_quality import canonical_state_hash, snapshot_freshness
from agent.runtime_confirmation import CONFIRMED_MUTATION_MODE, confirmation_summary
from scripts.create_shutdown_evidence import (
    ShutdownEvidenceError,
    resolve_bridge_relative_path,
    verify_published_shutdown_evidence,
)
from scripts import create_shutdown_evidence as shutdown_contract
from scripts import create_startup_consumption_evidence as startup_contract
from scripts import compare_live_safety_boundaries as boundary_compare
from scripts.finalize_priority_pair_lineage import _assess_frozen_confirmation
from scripts import finalize_priority_pair_lineage as priority_pair_contract
from scripts import configure_live_validation_runtime as runtime_config_core


_ACTIVE_PARENT_FINALIZE_LOCK: ContextVar[
    Optional[runtime_config_core.ActiveParentFinalizeLock]
] = ContextVar("active_parent_finalize_lock", default=None)

CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "capture-live-safety-boundary.ps1"
COMPARE_SCRIPT = PROJECT_ROOT / "scripts" / "compare_live_safety_boundaries.py"

ENVIRONMENT_SCHEMA = "oni.live-validation-environment.v1"
RUNTIME_BINDING_SCHEMA = "oni.live-validation-runtime-binding.v3"
FINALIZE_RESULT_SCHEMA = "oni.live-validation-finalize-result.v3"
FINALIZE_RECOVERY_SCHEMA = "oni.live-validation-finalize-recovery.v4"
FINALIZE_REVALIDATION_SCHEMA = "oni.live-validation-finalize-revalidation.v1"
FINALIZE_TRANSACTION_PLAN_SCHEMA = "oni.live-validation-finalize-transaction-plan.v1"
FINALIZE_TRANSACTION_RECOVERY_SCHEMA = "oni.live-validation-transaction-recovery.v2"
IMPLICIT_POST_COMMIT_REVALIDATION_STATE = "implicit_post_commit_revalidation"
ABORT_PREFLIGHT_SCHEMA = "oni.live-validation-abort-preflight.v1"
ABORT_RESULT_SCHEMA = "oni.live-validation-abort-result.v1"
ABORT_RECOVERY_SCHEMA = "oni.live-validation-abort-recovery.v1"
ABORT_TRANSACTION_PLAN_SCHEMA = "oni.live-validation-abort-transaction-plan.v1"
ABORT_RESULT_INTENT_SCHEMA = "oni.live-validation-abort-result-intent.v1"
ABORT_TRANSACTION_RECOVERY_SCHEMA = "oni.live-validation-abort-transaction-recovery.v1"
RUNTIME_CONFIG_BINDING_SCHEMA = "oni.live-validation-runtime-config-finalizer-binding.v1"
JOURNAL_SCHEMA = "oni.live-validation-restore-journal.v1"
JOURNAL_ENTRY_KEYS = frozenset(
    {
        "schema",
        "index",
        "recorded_at",
        "event",
        "data",
        "previous_entry_hash",
        "entry_hash",
    }
)
BOUNDARY_SCHEMA = "oni.live-safety-boundary.v1"
POST_BOUNDARY_COMPARISON_SCHEMA = "oni.live-safety-boundary-comparison.v2"
CLONE_SCHEMA = "oni.named-test-clone.v1"
LINEAGE_SCHEMA = "oni.test-save-lineage.v1"
BOOTSTRAP_SCHEMA = "oni.v27-runtime-bootstrap.v1"
PAIR_REPORT_SCHEMA = "oni.priority-pair-lineage-finalization.v1"
APPROVAL_BUNDLE_SCHEMA = "oni.confirmed-command-approval-bundle.v1"
COMMAND_FREEZE_SCHEMA = "oni.priority-command-lineage-freeze.v1"
PRIORITY_ACTION = "set_duplicant_chore_priority"

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
COMMAND_ID_RE = re.compile(r"^cmd-[A-Za-z0-9_.-]+$")
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PAIR_REPORT_KEYS = frozenset(
    {"schema", "status", "classification", "verification_mode", "fail_closed", "manifest", "pair", "final_tip", "shutdown", "report_hash"}
)
APPROVAL_BUNDLE_KEYS = frozenset(
    {
        "schema",
        "command_id",
        "action",
        "command_intent_hash",
        "final_command_hash",
        "final_command",
        "source_policy_hash",
        "source_policy",
        "approval_receipt_hash",
        "source_frame",
        "bundle_hash",
    }
)
COMMAND_FREEZE_KEYS = frozenset(
    {
        "schema",
        "step_id",
        "test_run_id",
        "manifest_binding",
        "approval_bundle",
        "confirmation_certificate",
        "confirmation_records",
        "frozen_at",
        "freeze_hash",
    }
)
RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{value}" for value in range(1, 10)),
    *(f"lpt{value}" for value in range(1, 10)),
}
CONFIG_RELATIVES = (
    "kplayerprefs.yaml",
    "mods/mods.json",
    "mods/mods.json.bak",
    "mods/config.json",
)
PROBE_RELATIVE = "mods/Dev/oni-ai-colony-agent"
BRIDGE_RELATIVE = "ai-bridge"
BRIDGE_ARCHIVE_RELATIVE = "ai-bridge-archives"
NONTERMINAL_QUEUES = ("pending", "approving", "approved", "executing", "confirming")
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_TREE_FILES = 100_000
MAX_TREE_BYTES = 4 * 1024 * 1024 * 1024
MAX_PIPELINE_STATE_AGE_SECONDS = 120.0
PAIR_PIPELINE_KEYS = frozenset(
    {
        "schema",
        "gate_hash",
        "inputs",
        "mutation_proposal",
        "counts",
        "command_count",
        "ambiguous_outcome_count",
        "duplicate_id_count",
        "stale_pending_count",
        "invalid_record_count",
    }
)
PAIR_PIPELINE_INPUT_KEYS = frozenset(
    {
        "latest_state_file_sha256",
        "latest_state_hash",
        "latest_state_core_hash",
        "health_file_sha256",
        "health_hash",
        "health_updated_at",
        "health_status",
        "terminal_publication",
        "bridge_projection",
        "startup_binding",
        "policy_file_sha256",
        "policy_hash",
        "terminal_archive",
        "bridge_replay",
        "companion_outputs",
        "command_tree_hash",
        "command_tree_entry_count",
        "snapshot_freshness",
    }
)
PAIR_PIPELINE_FRESHNESS_KEYS = frozenset(
    {
        "checked_at",
        "snapshot_ts",
        "sample_interval_seconds",
        "age_seconds",
        "threshold_seconds",
        "blockers",
    }
)
MAX_LEGACY_PATH_UTF16_UNITS = 260
REPARSE_ATTRIBUTE = 0x400
EXPECTED_RUNTIME_CONFIG_MOD_VERSION = "0.7.0"
RUNTIME_CONFIG_EVIDENCE_NAME = "runtime-config-evidence.json"
RUNTIME_CONFIG_FROZEN_RELATIVE = pathlib.PurePosixPath("pair/config.json")
FINALIZE_TEST_ONLY_OPTIONS = frozenset(
    {
        "test_fail_after_step",
        "test_finalize_result_fail_after_verify",
        "test_managed_assembly_csharp_sha256",
        "test_managed_assembly_csharp_firstpass_sha256",
    }
)
ABORT_TEST_ONLY_OPTIONS = frozenset(
    {
        "test_abort_fail_after_step",
        "test_abort_result_fail_after_verify",
        "test_abort_journal_fail_after_commit",
        "test_managed_assembly_csharp_sha256",
        "test_managed_assembly_csharp_firstpass_sha256",
    }
)
ABORT_RESULT_KEYS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "environment_manifest_sha256",
        "runtime_config_evidence_sha256",
        "startup_binding_sha256",
        "runtime_config_binding_hash",
        "preflight_sha256",
        "configuration_state",
        "diagnostic_file_count",
        "loader_pair_restored",
        "config_count",
        "probe_tree_restored",
        "old_bridge_restored",
        "runtime_diagnostics_archived",
        "physical_restore_committed",
        "transaction_id",
        "transaction_plan_hash",
        "final_state_hash",
        "physical_commit_entry_hash",
        "completed_at",
        "document_hash",
    }
)
ABORT_PREFLIGHT_KEYS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "environment_manifest_sha256",
        "runtime_config_evidence_sha256",
        "startup_binding_sha256",
        "runtime_config_binding_hash",
        "expected_release_dll_sha256",
        "managed_assembly_binding",
        "source_save",
        "loader_save",
        "loader_preview",
        "configuration_state",
        "runtime_bridge_snapshot_hash",
        "runtime_probe_snapshot_hash",
        "archive_root_exists",
        "archive_root_snapshot_hash",
        "diagnostic_file_count",
        "processes",
        "strict_pre_session_pre_action",
    }
)
RUNTIME_CONFIG_BINDING_KEYS = frozenset(
    {
        "schema",
        "source",
        "environment_manifest_sha256",
        "source_save",
        "loader_save",
        "probe",
        "bootstrap",
        "initial_disabled_policy",
        "release_dll",
        "loader_policy",
        "startup_intent",
        "startup_binding",
        "managed_assembly_binding",
        "desired_configs",
        "verification_report_hash",
    }
)
RUNTIME_BINDING_KEYS = frozenset(
    {
        "schema",
        "run_id",
        "environment_manifest_sha256",
        "runtime_config",
        "bootstrap",
        "lineage",
        "loader_before_restore",
        "priority_pair_report",
        "normal_shutdown_evidence",
        "shutdown_runtime_custody",
        "startup_consumption",
        "runtime_bridge_snapshot",
        "commands",
        "finalize_gate",
    }
)
STARTUP_CUSTODY_KEYS = frozenset(
    {
        "relative",
        "evidence_hash",
        "validation_process_id",
        "certificate_hash",
        "certificate",
        "bundle_custody",
        "bundle_custody_hash",
        "session_id",
        "validation_process_creation_token",
        "startup_archive_prefix_sha256",
        "startup_archive_prefix_length",
    }
)
SHUTDOWN_LINEAGE_KEYS = frozenset(
    {
        "path",
        "file_sha256",
        "test_run_id",
        "latest_checkpoint_hash",
        "origin",
        "activation",
        "activation_reload_archive",
        "checkpoints",
        "origin_archive",
        "final_session_id",
    }
)
SHUTDOWN_ACTIVATION_KEYS = frozenset(
    {
        "checkpoint_hash",
        "source_session_id",
        "source_save_id_hash",
        "reload_session_id",
        "reload_save_id_hash",
    }
)
SHUTDOWN_CHECKPOINT_KEYS = frozenset(
    {
        "index",
        "step_id",
        "checkpoint_hash",
        "before_session_id",
        "before_save_id_hash",
        "reload_session_id",
        "reload_save_id_hash",
    }
)
NORMAL_SHUTDOWN_BINDING_KEYS = frozenset(
    {
        "path_id_lower",
        "file_sha256",
        "evidence_hash",
        "source_path_id_lower",
        "source_file_sha256",
    }
)
SHUTDOWN_RUNTIME_CUSTODY_KEYS = frozenset(
    {
        "schema",
        "lineage_origin",
        "lineage_origin_hash",
        "final_session_id",
        "lineage_manifest",
        "origin_archive",
        "activation_reload_archive",
        "final_event_log",
        "custody_hash",
    }
)
SHUTDOWN_RUNTIME_CUSTODY_SCHEMA = "oni.live-validation-shutdown-runtime-custody.v2"
SHUTDOWN_RUNTIME_FILE_CUSTODY_KEYS = frozenset(
    {
        "relative",
        "logical_path_id_lower",
        "identity",
        "length",
        "last_write_ns",
        "sha256",
    }
)
POST_BOUNDARY_REPORT_KEYS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "blockers",
        "preexisting_save_count",
        "new_save_count",
        "unexpected_added_saves",
        "incomplete_allowed_pairs",
        "save_diff",
        "mod_config_diff",
        "installed_probe_diff",
        "bridge_policy_diff",
        "nonterminal_queues",
        "temporary_file_count",
        "processes",
        "pre_report_sha256",
        "post_report_sha256",
        "pre_report_relative",
        "post_report_relative",
        "pre_report_path_id_lower",
        "post_report_path_id_lower",
        "capture_receipt",
        "capture_receipt_hash",
        "comparison_engine",
        "allowed_loader_stem",
    }
)
FINALIZE_RESULT_KEYS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "environment_manifest_sha256",
        "runtime_binding_sha256",
        "runtime_config",
        "runtime_config_binding_hash",
        "runtime_config_evidence_sha256",
        "startup_binding_sha256",
        "expected_release_dll_sha256",
        "command_count",
        "normal_shutdown_evidence_hash",
        "startup_consumption_evidence_hash",
        "startup_certificate_projection_hash",
        "startup_bundle_custody_hash",
        "validation_process_id",
        "startup_session_id",
        "validation_process_creation_token",
        "startup_archive_prefix_sha256",
        "startup_archive_prefix_length",
        "derivative_pair_archived",
        "loader_pair_restored",
        "config_count",
        "probe_tree_restored",
        "old_bridge_restored",
        "post_boundary_status",
        "post_boundary_report_sha256",
        "allowed_new_save_stem_count",
        "document_hash",
    }
)
FINALIZE_RECOVERY_KEYS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "environment_manifest_sha256",
        "state",
        "physical_commit_non_replay",
        "recovery_required",
        "error_type",
        "runtime_binding_sha256",
        "runtime_config",
        "runtime_config_binding_hash",
        "runtime_config_evidence_sha256",
        "startup_binding_sha256",
        "expected_release_dll_sha256",
        "normal_shutdown_evidence_hash",
        "startup_consumption_evidence_hash",
        "startup_certificate_projection_hash",
        "startup_bundle_custody_hash",
        "validation_process_id",
        "startup_session_id",
        "validation_process_creation_token",
        "startup_archive_prefix_sha256",
        "startup_archive_prefix_length",
        "candidate_result",
        "candidate_result_hash",
        "document_hash",
    }
)
FINALIZE_REVALIDATION_KEYS = frozenset(
    {
        "schema",
        "status",
        "run_id",
        "environment_manifest_sha256",
        "runtime_binding_sha256",
        "source_recovery_sha256",
        "source_recovery_state",
        "post_boundary_report_sha256",
        "candidate_result",
        "candidate_result_hash",
        "document_hash",
    }
)


class WorkflowError(RuntimeError):
    """Fail-closed workflow error."""


def reject_test_only_finalize_options(
    args: argparse.Namespace,
    *,
    test_root_present: bool,
) -> None:
    """Reject every finalize fault-injection option outside TestRoot mode."""

    enabled = sorted(
        option
        for option in FINALIZE_TEST_ONLY_OPTIONS
        if bool(getattr(args, option, None))
    )
    if enabled and not test_root_present:
        flags = ", ".join(f"--{option.replace('_', '-')}" for option in enabled)
        raise WorkflowError(
            "TestRoot-only finalize options are forbidden in live mode: " + flags
        )


def reject_test_only_abort_options(
    args: argparse.Namespace,
    *,
    test_root_present: bool,
) -> None:
    """Reject every abort fault/fixture override outside TestRoot mode."""

    enabled = sorted(
        option
        for option in ABORT_TEST_ONLY_OPTIONS
        if bool(getattr(args, option, None))
    )
    if enabled and not test_root_present:
        flags = ", ".join(f"--{option.replace('_', '-')}" for option in enabled)
        raise WorkflowError(
            "TestRoot-only abort options are forbidden in live mode: " + flags
        )


class PostValidationError(WorkflowError):
    """The environment is restored, but post-boundary acceptance failed."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _io_path(path: pathlib.Path | str) -> str:
    """Return an OS-facing path without changing the workflow's logical path."""

    text = os.fspath(path)
    if os.name != "nt":
        return text
    text = os.path.abspath(os.path.normpath(text))
    if text.startswith(("\\\\?\\", "\\\\.\\")):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _lstat(path: pathlib.Path | str) -> os.stat_result:
    return os.lstat(_io_path(path))


def _stable_file_snapshot(
    path: pathlib.Path,
    *,
    max_bytes: Optional[int] = None,
    collect_bytes: bool = False,
    after_lstat_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_read_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> dict[str, Any]:
    """Read one single-link regular file through a path-bound descriptor."""

    path = pathlib.Path(os.path.abspath(os.path.normpath(str(pathlib.Path(path).expanduser()))))
    io_path = _io_path(path)
    try:
        before = os.lstat(io_path)
    except OSError as exc:
        raise WorkflowError("stable file source is missing") from exc

    def identity(metadata: os.stat_result) -> tuple[int, int, int]:
        return (int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode))

    def stable_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            *identity(metadata),
            int(metadata.st_nlink),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )

    if (
        stat.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (max_bytes is not None and before.st_size > max_bytes)
    ):
        raise WorkflowError("stable file source is unsafe, hardlinked, or too large")
    if after_lstat_hook is not None:
        after_lstat_hook(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(io_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if stable_metadata(opened) != stable_metadata(before):
                raise WorkflowError(f"stable file pathname changed before descriptor open: {path}")
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            length = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                length += len(chunk)
                if max_bytes is not None and length > max_bytes:
                    raise WorkflowError("stable file source exceeds its size bound")
                digest.update(chunk)
                if collect_bytes:
                    chunks.append(chunk)
            if after_read_hook is not None:
                after_read_hook(path)
            handle.seek(0)
            verification_digest = hashlib.sha256()
            verification_length = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                verification_length += len(chunk)
                verification_digest.update(chunk)
            after_handle = os.fstat(handle.fileno())
        after_path = os.lstat(io_path)
    except OSError as exc:
        raise WorkflowError("stable file source could not be read") from exc
    if (
        stable_metadata(after_handle) != stable_metadata(opened)
        or stable_metadata(after_path) != stable_metadata(before)
        or int(after_handle.st_ctime_ns) != int(opened.st_ctime_ns)
        or int(after_path.st_ctime_ns) != int(before.st_ctime_ns)
        or length != opened.st_size
        or verification_length != length
        or verification_digest.digest() != digest.digest()
    ):
        raise WorkflowError(f"stable file source changed during descriptor read: {path}")
    return {
        "identity": list(identity(opened)),
        "length": length,
        "last_write_ns": int(opened.st_mtime_ns),
        "sha256": "sha256:" + digest.hexdigest(),
        "bytes": b"".join(chunks) if collect_bytes else None,
    }


def sha256_file(path: pathlib.Path) -> str:
    return str(_stable_file_snapshot(path)["sha256"])


def _expand_short_names(text: str) -> str:
    """Spell an existing prefix with long names instead of 8.3 aliases.

    ``C:\\Users\\user\\DOCUME~1`` and ``...\\Documents`` are one directory; string
    comparisons and path ids must not treat them as disjoint (review section
    6).  GetLongPathNameW expands aliases without following junctions, so the
    reparse-point checks still see every segment.  Aliases always contain "~".
    """
    if os.name != "nt" or "~" not in text:
        return text
    head, tail = text, []
    while not os.path.exists(head):
        parent, name = os.path.split(head)
        if parent == head or not name:
            return text
        tail.append(name)
        head = parent
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetLongPathNameW(head, buffer, len(buffer))
    if not length or length >= len(buffer):
        raise WorkflowError("a path could not be expanded to its long form")
    return os.path.join(buffer.value, *reversed(tail))


def normalized(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(
        _expand_short_names(os.path.abspath(os.path.normpath(str(pathlib.Path(path).expanduser()))))
    )


def canonical_path_text(path: pathlib.Path | str) -> str:
    return os.path.normcase(str(normalized(path)).rstrip("\\/"))


def same_path(left: pathlib.Path | str, right: pathlib.Path | str) -> bool:
    return canonical_path_text(left) == canonical_path_text(right)


def path_within(path: pathlib.Path | str, root: pathlib.Path | str, *, strict: bool = True) -> bool:
    candidate = canonical_path_text(path)
    boundary = canonical_path_text(root)
    try:
        common = os.path.commonpath((candidate, boundary))
    except ValueError:
        return False
    return common == boundary and (not strict or candidate != boundary)


def paths_overlap(left: pathlib.Path | str, right: pathlib.Path | str) -> bool:
    return path_within(left, right, strict=False) or path_within(right, left, strict=False)


def path_id(path: pathlib.Path | str, *, contract: str) -> str:
    text = str(normalized(path)).rstrip("\\/")
    if contract == "lower":
        text = text.lower()
    elif contract == "upper":
        text = text.upper()
    else:
        raise WorkflowError("unsupported path-id contract")
    return sha256_bytes(text.encode("utf-8"))


def text_id(value: str) -> str:
    return sha256_bytes(value.lower().encode("utf-8"))


def validate_windows_segment(value: str, label: str) -> None:
    if not value or value in {".", ".."} or value.endswith((" ", ".")):
        raise WorkflowError(f"{label} contains an unsafe Windows path segment")
    if ":" in value or "\x00" in value:
        raise WorkflowError(f"{label} contains an unsafe Windows path segment")
    if value.split(".", 1)[0].lower() in RESERVED_WINDOWS_NAMES:
        raise WorkflowError(f"{label} uses a reserved Windows device name")


def validate_relative_path(value: str, label: str) -> pathlib.PurePosixPath:
    relative = pathlib.PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise WorkflowError(f"{label} is not a safe relative path")
    for part in relative.parts:
        validate_windows_segment(part, label)
    return relative


def is_reparse(path: pathlib.Path) -> bool:
    metadata = _lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    )


def assert_no_reparse(path: pathlib.Path, boundary: pathlib.Path, *, allow_missing_leaf: bool = False) -> None:
    path = normalized(path)
    boundary = normalized(boundary)
    if not path_within(path, boundary, strict=False):
        raise WorkflowError("path escaped its declared safety boundary")
    if not boundary.exists() or not boundary.is_dir() or is_reparse(boundary):
        raise WorkflowError("safety boundary is missing or is a reparse point")
    relative = pathlib.Path(os.path.relpath(path, boundary))
    current = boundary
    for index, part in enumerate(relative.parts):
        if part == ".":
            continue
        validate_windows_segment(part, "path")
        current = current / part
        if os.path.lexists(current):
            if is_reparse(current):
                raise WorkflowError("reparse points are forbidden in workflow paths")
        elif not (allow_missing_leaf and index == len(relative.parts) - 1):
            raise WorkflowError("required workflow path is missing")


def assert_safe_root(path: pathlib.Path, label: str) -> pathlib.Path:
    root = normalized(path)
    text = str(root)
    if text.startswith("\\") or root.parent == root:
        raise WorkflowError(f"{label} cannot be UNC or a filesystem root")
    if not root.exists() or not root.is_dir():
        raise WorkflowError(f"{label} must be an existing directory")
    # Checking only the leaf misses a junction in an ancestor (for example a
    # redirected fixture parent).  Walk the existing absolute ancestry so all
    # workflow roots have an unambiguous local-filesystem identity.
    current = pathlib.Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        if os.path.lexists(current) and is_reparse(current):
            raise WorkflowError(f"{label} ancestry contains a reparse point")
    assert_no_reparse(root, root)
    return root


def same_volume(*paths: pathlib.Path) -> bool:
    drives = {os.path.splitdrive(str(normalized(path)))[0].casefold() for path in paths}
    return len(drives) == 1


def _rename_create_only(source: pathlib.Path, target: pathlib.Path, label: str) -> None:
    """Publish by rename without overwriting a concurrently-created target."""

    source = normalized(source)
    target = normalized(target)
    if os.name != "nt":
        raise WorkflowError(f"{label} create-only rename requires Windows no-replace semantics")
    try:
        os.rename(source, target)
    except FileExistsError as exc:
        raise WorkflowError(f"{label} target appeared before create-only publication") from exc
    except OSError as exc:
        # Windows rename is non-overwriting.  Preserve both names and classify
        # every collision/ambiguous failure fail-closed.
        raise WorkflowError(f"{label} create-only publication failed") from exc


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise WorkflowError("JSON contains a duplicate object key")
        value[key] = item
    return value


def load_json(
    path: pathlib.Path,
    label: str,
    *,
    after_lstat_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_read_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> dict[str, Any]:
    try:
        snapshot = _stable_file_snapshot(
            path,
            max_bytes=MAX_JSON_BYTES,
            collect_bytes=True,
            after_lstat_hook=after_lstat_hook,
            after_read_hook=after_read_hook,
        )
        raw = snapshot["bytes"]
        if not isinstance(raw, bytes):
            raise WorkflowError(f"{label} has no stable JSON payload")
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                WorkflowError(f"non-finite JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} root must be an object")
    return value


def load_json_with_sha256(
    path: pathlib.Path,
    label: str,
) -> tuple[dict[str, Any], str]:
    """Parse one descriptor-stable JSON snapshot and return its raw hash."""

    try:
        snapshot = _stable_file_snapshot(
            path,
            max_bytes=MAX_JSON_BYTES,
            collect_bytes=True,
        )
        raw = snapshot.get("bytes")
        if not isinstance(raw, bytes):
            raise WorkflowError(f"{label} has no stable JSON payload")
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                WorkflowError(f"non-finite JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} root must be an object")
    return value, str(snapshot["sha256"])


def _owned_node_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode))


def _path_matches_owned_identity(
    path: pathlib.Path,
    identity: Optional[tuple[int, int, int]],
    *,
    kind: str,
    single_link: bool = False,
) -> bool:
    if identity is None:
        return False
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    ):
        return False
    if kind == "file" and not stat.S_ISREG(metadata.st_mode):
        return False
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        return False
    return _owned_node_identity(metadata) == identity and (not single_link or metadata.st_nlink == 1)


def _restore_mismatched_claim(path: pathlib.Path, claim: pathlib.Path, label: str) -> None:
    try:
        if os.path.lexists(path):
            raise WorkflowError(f"{label} replacement retained in the private cleanup claim")
        os.rename(claim, path)
    except OSError as exc:
        raise WorkflowError(f"{label} replacement retained in the private cleanup claim") from exc


def _claim_owned_node(
    path: pathlib.Path,
    identity: Optional[tuple[int, int, int]],
    *,
    kind: str,
    single_link: bool = False,
    before_claim_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> pathlib.Path:
    """Atomically remove a public name before deciding whether it is ours."""

    path = normalized(path)
    claim = path.parent / f".{path.name[:24]}.cleanup-{uuid.uuid4().hex[:16]}"
    if os.path.lexists(claim):
        raise WorkflowError("private cleanup claim already exists")
    if before_claim_hook is not None:
        before_claim_hook(path)
    try:
        os.rename(path, claim)
    except FileNotFoundError:
        raise WorkflowError("owned cleanup path disappeared")
    except OSError as exc:
        raise WorkflowError("owned cleanup path could not be atomically claimed") from exc
    if not _path_matches_owned_identity(claim, identity, kind=kind, single_link=single_link):
        _restore_mismatched_claim(path, claim, "owned cleanup")
        raise WorkflowError("owned cleanup identity mismatch; replacement restored")
    return claim


def _unlink_owned_file(
    path: pathlib.Path,
    identity: Optional[tuple[int, int, int]],
    *,
    before_claim_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> bool:
    if identity is None or not os.path.lexists(path):
        return False
    claim = _claim_owned_node(
        path,
        identity,
        kind="file",
        single_link=True,
        before_claim_hook=before_claim_hook,
    )
    # The claim is a fresh, random, transaction-private pathname.  Recheck it
    # immediately before deletion; a mismatch is retained, never unlinked.
    if not _path_matches_owned_identity(claim, identity, kind="file", single_link=True):
        raise WorkflowError("owned cleanup claim changed; recovery file retained")
    claim.unlink()
    return True


def _unlink_owned_publication_link(
    temporary: pathlib.Path,
    target: pathlib.Path,
    identity: Optional[tuple[int, int, int]],
) -> None:
    """Drop the private half of a two-link create-only publication safely."""

    claim = _claim_owned_node(temporary, identity, kind="file")
    try:
        claim_metadata = os.lstat(claim)
        target_metadata = os.lstat(target)
    except OSError as exc:
        raise WorkflowError("create-only publication link custody became unreadable") from exc
    if (
        _owned_node_identity(claim_metadata) != identity
        or _owned_node_identity(target_metadata) != identity
        or claim_metadata.st_nlink != 2
        or target_metadata.st_nlink != 2
    ):
        raise WorkflowError("create-only publication link custody is ambiguous; recovery retained")
    claim.unlink()
    if not _path_matches_owned_identity(target, identity, kind="file", single_link=True):
        raise WorkflowError("create-only publication target did not become single-link")


def _delete_owned_tree_claim(claim: pathlib.Path, expected: Mapping[str, Any]) -> None:
    """Delete exactly the captured tree; never recursively follow new nodes."""

    if not transaction_node_matches(claim, expected):
        raise WorkflowError("owned cleanup tree changed before deletion; recovery tree retained")
    identities = {
        str(item[0]): (int(item[2]), int(item[3]), int(item[4]))
        for item in expected.get("node_identities", [])
        if isinstance(item, (list, tuple)) and len(item) == 5
    }
    entries = [item for item in identities if item != "."]
    kinds = {
        str(item[0]): str(item[1])
        for item in expected.get("node_identities", [])
        if isinstance(item, (list, tuple)) and len(item) == 5
    }
    # Rely on the captured kind rather than suffix for extensionless files.
    files = [item for item in entries if kinds.get(item) == "file"]
    directories = sorted(
        (item for item in entries if kinds.get(item) == "directory"),
        key=lambda item: len(pathlib.PurePosixPath(item).parts),
        reverse=True,
    )
    for relative in files:
        node = claim / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        _unlink_owned_file(node, identities[relative])
    for relative in directories:
        node = claim / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        node_claim = _claim_owned_node(node, identities[relative], kind="directory")
        try:
            node_claim.rmdir()
        except OSError as exc:
            raise WorkflowError("owned cleanup directory gained an unexpected child; recovery retained") from exc
    root_identity = identities.get(".")
    if not _path_matches_owned_identity(claim, root_identity, kind="directory"):
        raise WorkflowError("owned cleanup root identity changed; recovery retained")
    try:
        claim.rmdir()
    except OSError as exc:
        raise WorkflowError("owned cleanup root gained an unexpected child; recovery retained") from exc


def _remove_owned_tree(
    path: pathlib.Path,
    expected: Optional[Mapping[str, Any]],
    *,
    before_claim_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> bool:
    if expected is None or not os.path.lexists(path):
        return False
    identity_value = expected.get("identity")
    identity = (
        tuple(int(item) for item in identity_value)
        if isinstance(identity_value, (list, tuple)) and len(identity_value) == 3
        else None
    )
    claim = _claim_owned_node(
        path,
        identity,
        kind="directory",
        before_claim_hook=before_claim_hook,
    )
    if not transaction_node_matches(claim, expected):
        _restore_mismatched_claim(path, claim, "owned cleanup tree")
        raise WorkflowError("owned cleanup tree mismatch; replacement restored")
    _delete_owned_tree_claim(claim, expected)
    return True


def write_json_atomic(
    path: pathlib.Path,
    value: Mapping[str, Any],
    *,
    create_only: bool = False,
    before_publish_hook: Optional[Callable[[pathlib.Path, pathlib.Path], None]] = None,
    before_link_unlink_hook: Optional[Callable[[pathlib.Path, pathlib.Path], None]] = None,
    after_publish_hook: Optional[Callable[[pathlib.Path, pathlib.Path], None]] = None,
    after_verify_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> None:
    path = normalized(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the sibling name short.  Repeating a potentially long evidence file
    # name plus a full UUID can cross the legacy Win32 MAX_PATH boundary even
    # when the committed path itself is safe.
    temporary = path.parent / f".j-{uuid.uuid4().hex[:16]}.tmp"
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    payload_hash = sha256_bytes(payload)
    temporary_identity: Optional[tuple[int, int, int]] = None
    published_identity: Optional[tuple[int, int, int]] = None
    transferred = False
    published = False
    try:
        try:
            handle = temporary.open("x+b")
        except FileExistsError as exc:
            raise WorkflowError("atomic JSON staging path already exists") from exc
        with handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise WorkflowError("atomic JSON staging file is unsafe")
            temporary_identity = _owned_node_identity(opened)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno())
            if (
                _owned_node_identity(written) != temporary_identity
                or written.st_nlink != 1
                or written.st_size != len(payload)
            ):
                raise WorkflowError("atomic JSON staging descriptor changed")
        if not _path_matches_owned_identity(temporary, temporary_identity, kind="file", single_link=True):
            raise WorkflowError("atomic JSON staging path ownership changed")
        if sha256_file(temporary) != payload_hash:
            raise WorkflowError("atomic JSON staging payload hash mismatch")
        if before_publish_hook is not None:
            before_publish_hook(path, temporary)
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise WorkflowError("immutable evidence path already exists") from exc
            published_identity = temporary_identity
            published = True
            target_metadata = os.lstat(path)
            temp_metadata = os.lstat(temporary)
            if (
                _owned_node_identity(target_metadata) != temporary_identity
                or _owned_node_identity(temp_metadata) != temporary_identity
                or target_metadata.st_nlink != 2
                or temp_metadata.st_nlink != 2
            ):
                raise WorkflowError("create-only JSON link publication ownership is ambiguous")
            if before_link_unlink_hook is not None:
                before_link_unlink_hook(path, temporary)
            _unlink_owned_publication_link(temporary, path, temporary_identity)
            transferred = True
        else:
            _rename_create_only(temporary, path, "atomic JSON")
            transferred = True
            published_identity = temporary_identity
            published = True
        if after_publish_hook is not None:
            after_publish_hook(path, temporary)
        if (
            not _path_matches_owned_identity(path, published_identity, kind="file", single_link=True)
            or path.stat().st_size != len(payload)
            or sha256_file(path) != payload_hash
        ):
            raise WorkflowError("atomic JSON publication identity/hash mismatch")
        if after_verify_hook is not None:
            after_verify_hook(path)
        if not _path_matches_owned_identity(path, published_identity, kind="file", single_link=True):
            raise WorkflowError("atomic JSON publication changed after verification")
    except Exception as exc:
        removed_owned_target = False
        if not transferred and os.path.lexists(temporary):
            try:
                _unlink_owned_file(temporary, temporary_identity)
            except WorkflowError as cleanup_exc:
                raise WorkflowError("atomic JSON staging cleanup outcome_unknown; recovery retained") from cleanup_exc
        if published and os.path.lexists(path):
            try:
                removed_owned_target = _unlink_owned_file(path, published_identity)
            except WorkflowError as cleanup_exc:
                raise WorkflowError("atomic JSON publication revoke outcome_unknown; replacement retained") from cleanup_exc
        if published and not removed_owned_target and not _path_matches_owned_identity(
            path, published_identity, kind="file", single_link=True
        ):
            raise WorkflowError("atomic JSON publication outcome_unknown; replacement retained") from exc
        raise


def write_json_once(path: pathlib.Path, value: Mapping[str, Any], label: str) -> None:
    """Accept one exact immutable publication, otherwise create it once."""

    path = normalized(path)
    if os.path.lexists(path):
        if load_json(path, label) != dict(value):
            raise WorkflowError(f"{label} conflicts with an existing immutable publication")
        return
    write_json_atomic(path, value, create_only=True)


def write_json_once_by_rename(
    path: pathlib.Path,
    value: Mapping[str, Any],
    label: str,
    *,
    after_verify_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> None:
    """Create one exact JSON document without a hard-link crash window.

    ``write_json_atomic(..., create_only=True)`` deliberately exercises a
    hard-link publication protocol used by older evidence. A process death
    between link and unlink can, however, leave the target and random staging
    name attached to the same inode. Finalization/recovery publications must
    be retry-convergent, so use Windows' non-overwriting rename primitive and
    accept an already-published file only when its exact writer bytes match.
    """

    path = normalized(path)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if os.path.lexists(path):
        snapshot = _stable_file_snapshot(
            path,
            max_bytes=MAX_JSON_BYTES,
            collect_bytes=True,
        )
        if snapshot.get("bytes") != payload:
            raise WorkflowError(f"{label} conflicts with an existing immutable publication")
        return
    write_json_atomic(
        path,
        value,
        create_only=False,
        after_verify_hook=after_verify_hook,
    )


def file_state(path: pathlib.Path) -> dict[str, Any]:
    snapshot = _stable_file_snapshot(path)
    return {
        "length": snapshot["length"],
        "last_write_ns": snapshot["last_write_ns"],
        "sha256": snapshot["sha256"],
    }


def optional_file_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    return {"exists": True, "state": file_state(path)}


def states_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return dict(left) == dict(right)


def snapshot_tree(root: pathlib.Path) -> dict[str, Any]:
    root = normalized(root)
    if not os.path.lexists(_io_path(root)):
        return {"exists": False, "entries": [], "tree_hash": sha256_bytes(b"[]")}
    root_metadata = _lstat(root)
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode) or bool(
        getattr(root_metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    ):
        raise WorkflowError("tree root must be a non-reparse directory")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    io_root = pathlib.Path(_io_path(root))
    logical_paths = [
        root.joinpath(*io_entry.relative_to(io_root).parts)
        for io_entry in io_root.rglob("*")
    ]
    for path in sorted(logical_paths, key=lambda item: str(item).casefold()):
        metadata = _lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
        ):
            raise WorkflowError("reparse points are forbidden inside captured trees")
        relative = path.relative_to(root).as_posix()
        validate_relative_path(relative, "tree entry")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append(
                {
                    "kind": "directory",
                    "relative": relative,
                    "last_write_ns": metadata.st_mtime_ns,
                }
            )
        elif stat.S_ISREG(metadata.st_mode):
            state_value = file_state(path)
            total_bytes += int(state_value["length"])
            entries.append({"kind": "file", "relative": relative, **state_value})
        else:
            raise WorkflowError("captured trees may contain only files and directories")
        if len(entries) > MAX_TREE_FILES or total_bytes > MAX_TREE_BYTES:
            raise WorkflowError("captured tree exceeds the safety bound")
    root_metadata = _lstat(root)
    payload = {
        "exists": True,
        "root_last_write_ns": root_metadata.st_mtime_ns,
        "entries": entries,
        "file_count": sum(item["kind"] == "file" for item in entries),
        "total_bytes": total_bytes,
    }
    payload["tree_hash"] = sha256_bytes(canonical_bytes(payload))
    return payload


def verify_tree(root: pathlib.Path, expected: Mapping[str, Any], label: str) -> None:
    actual = snapshot_tree(root)
    if dict(actual) != dict(expected):
        raise WorkflowError(f"{label} tree no longer matches its bound evidence")


def verify_shutdown_snapshot_bindings(
    bridge: pathlib.Path,
    bridge_snapshot: Mapping[str, Any],
    shutdown_evidence: Mapping[str, Any],
) -> None:
    """Cross-bind every shutdown proof source to one captured bridge tree."""

    bridge = normalized(bridge)
    document = shutdown_evidence.get("document")
    if not isinstance(document, Mapping) or bridge_snapshot.get("exists") is not True:
        raise WorkflowError("normal shutdown evidence has no bindable runtime bridge snapshot")
    expected_files: list[tuple[str, pathlib.Path, str]] = [
        (
            "shutdown evidence",
            normalized(pathlib.Path(str(shutdown_evidence.get("path") or ""))),
            str(shutdown_evidence.get("file_sha256") or ""),
        ),
        (
            "shutdown source log",
            normalized(pathlib.Path(str(shutdown_evidence.get("source_path") or ""))),
            str(shutdown_evidence.get("source_file_sha256") or ""),
        ),
    ]
    lineage = document.get("lineage") if isinstance(document.get("lineage"), Mapping) else {}
    report = (
        document.get("priority_pair_report")
        if isinstance(document.get("priority_pair_report"), Mapping)
        else {}
    )
    expected_files.extend(
        (
            ("lineage manifest", normalized(pathlib.Path(str(lineage.get("path") or ""))), str(lineage.get("file_sha256") or "")),
            ("priority pair report", normalized(pathlib.Path(str(report.get("path") or ""))), str(report.get("file_sha256") or "")),
        )
    )
    pair = document.get("pair") if isinstance(document.get("pair"), Mapping) else {}
    commands = pair.get("commands") if isinstance(pair.get("commands"), list) else []
    if len(commands) != 2:
        raise WorkflowError("normal shutdown evidence command sources are incomplete")
    for command in commands:
        if not isinstance(command, Mapping):
            raise WorkflowError("normal shutdown evidence command source is invalid")
        step_id = str(command.get("step_id") or "unknown")
        bundle = (
            command.get("automatic_approval_bundle")
            if isinstance(command.get("automatic_approval_bundle"), Mapping)
            else {}
        )
        freeze = command.get("lineage_freeze") if isinstance(command.get("lineage_freeze"), Mapping) else {}
        expected_files.extend(
            (
                (
                    f"{step_id} approval bundle",
                    normalized(pathlib.Path(str(bundle.get("path") or ""))),
                    str(bundle.get("file_sha256") or ""),
                ),
                (
                    f"{step_id} lineage freeze",
                    normalized(pathlib.Path(str(freeze.get("path") or ""))),
                    str(freeze.get("file_sha256") or ""),
                ),
            )
        )

    snapshot_files = {
        str(item.get("relative")): item
        for item in bridge_snapshot.get("entries", [])
        if isinstance(item, Mapping) and item.get("kind") == "file"
    }
    seen: dict[str, str] = {}
    for label, path, expected_hash in expected_files:
        if not path_within(path, bridge) or HASH_RE.fullmatch(expected_hash) is None:
            raise WorkflowError(f"{label} is outside the runtime bridge or has no trusted hash")
        relative_text = pathlib.Path(os.path.relpath(path, bridge)).as_posix()
        relative = validate_relative_path(relative_text, label).as_posix()
        prior_hash = seen.setdefault(canonical_path_text(path), expected_hash)
        if prior_hash != expected_hash:
            raise WorkflowError(f"{label} has conflicting shutdown evidence hashes")
        captured = snapshot_files.get(relative)
        if not isinstance(captured, Mapping) or captured.get("sha256") != expected_hash:
            raise WorkflowError(f"{label} does not match the runtime bridge snapshot")


def verify_command_snapshot_bindings(
    bridge: pathlib.Path,
    bridge_snapshot: Mapping[str, Any],
    command_evidence: Sequence[Mapping[str, Any]],
) -> None:
    snapshot_files = {
        str(item.get("relative")): item
        for item in bridge_snapshot.get("entries", [])
        if isinstance(item, Mapping) and item.get("kind") == "file"
    }
    for command in command_evidence:
        step_id = str(command.get("step_id") or "unknown")
        bindings = [
            ("done command", "command_path", "command_file_sha256"),
            ("approval bundle", "bundle_path", "bundle_file_sha256"),
            ("lineage freeze", "freeze_path", "freeze_file_sha256"),
        ]
        optional_bindings = [
            (
                "source frame",
                "source_frame_path",
                "source_frame_archive_file_sha256",
                "source_frame_currently_present",
            ),
            (
                "confirmation certificate",
                "confirmation_certificate_path",
                "confirmation_certificate_file_sha256",
                "confirmation_certificate_currently_present",
            ),
        ]
        for label, path_key, hash_key in bindings:
            path = normalized(pathlib.Path(str(command.get(path_key) or "")))
            expected_hash = str(command.get(hash_key) or "")
            if not path_within(path, bridge) or HASH_RE.fullmatch(expected_hash) is None:
                raise WorkflowError(f"{step_id} {label} escaped the runtime bridge snapshot")
            relative = validate_relative_path(
                pathlib.Path(os.path.relpath(path, bridge)).as_posix(), f"{step_id} {label}"
            ).as_posix()
            captured = snapshot_files.get(relative)
            if not isinstance(captured, Mapping) or captured.get("sha256") != expected_hash:
                raise WorkflowError(f"{step_id} {label} does not match the runtime bridge snapshot")
        for label, path_key, hash_key, present_key in optional_bindings:
            path = normalized(pathlib.Path(str(command.get(path_key) or "")))
            expected_hash = str(command.get(hash_key) or "")
            if not path_within(path, bridge) or HASH_RE.fullmatch(expected_hash) is None:
                raise WorkflowError(f"{step_id} {label} escaped the runtime bridge snapshot")
            relative = validate_relative_path(
                pathlib.Path(os.path.relpath(path, bridge)).as_posix(), f"{step_id} {label}"
            ).as_posix()
            captured = snapshot_files.get(relative)
            present = command.get(present_key)
            if present is True and (
                not isinstance(captured, Mapping) or captured.get("sha256") != expected_hash
            ):
                raise WorkflowError(f"{step_id} {label} does not match the runtime bridge snapshot")
            if present is False and captured is not None:
                raise WorkflowError(f"{step_id} absent {label} unexpectedly exists in the runtime bridge snapshot")
            if present is not True and present is not False:
                raise WorkflowError(f"{step_id} {label} presence is ambiguous")


def verify_archive_remainder(
    archive_root: pathlib.Path,
    expected: Mapping[str, Any],
    excluded_archive: Optional[pathlib.Path],
) -> None:
    current = snapshot_tree(archive_root)
    if excluded_archive is None:
        if bool(current.get("exists")) != bool(expected.get("exists")):
            raise WorkflowError("bridge archive root existence changed after prepare")
        current_entries = list(current.get("entries") or [])
    else:
        if not current.get("exists") or not path_within(excluded_archive, archive_root):
            raise WorkflowError("bootstrap old-bridge archive is outside the archive tree")
        if normalized(excluded_archive).parent != normalized(archive_root):
            raise WorkflowError("bootstrap old-bridge archive must be a direct archive-root child")
        prefix = excluded_archive.name
        entries = list(current.get("entries") or [])
        if not any(item.get("kind") == "directory" and item.get("relative") == prefix for item in entries):
            raise WorkflowError("bootstrap old-bridge archive is missing from the archive tree")
        current_entries = [
            item
            for item in entries
            if item.get("relative") != prefix
            and not str(item.get("relative") or "").startswith(prefix + "/")
        ]
    expected_entries = list(expected.get("entries") or []) if expected.get("exists") else []
    current_file_count = sum(item.get("kind") == "file" for item in current_entries)
    current_total_bytes = sum(int(item.get("length", 0)) for item in current_entries if item.get("kind") == "file")
    if (
        current_entries != expected_entries
        or current_file_count != int(expected.get("file_count", 0))
        or current_total_bytes != int(expected.get("total_bytes", 0))
    ):
        raise WorkflowError("preexisting bridge archive entries changed or an unexpected entry appeared")


def copy_file_stable(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    before_commit_hook: Optional[Callable[[], None]] = None,
    before_temporary_open_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_copy_chunk_hook: Optional[Callable[[pathlib.Path, int], None]] = None,
    published_owner_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    after_publish_hook: Optional[Callable[[pathlib.Path, pathlib.Path], None]] = None,
) -> dict[str, Any]:
    """Copy one file without overwrite and reject a concurrent source change.

    ``before_commit_hook`` is intentionally API-only and is used by the offline
    verifier to deterministically exercise the concurrency refusal boundary.
    """

    source = normalized(source)
    target = normalized(target)
    if target.exists() or os.path.lexists(target):
        raise WorkflowError("copy target already exists")
    before_snapshot = _stable_file_snapshot(source)
    before = {
        "length": int(before_snapshot["length"]),
        "last_write_ns": int(before_snapshot["last_write_ns"]),
        "sha256": str(before_snapshot["sha256"]),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    # CopyFile2 still reports WinError 3 for some long, non-prefixed paths.
    # A short sibling avoids manufacturing that failure during atomic staging.
    temporary = target.parent / f".c-{uuid.uuid4().hex[:16]}.tmp"
    temporary_identity: Optional[tuple[int, int, int]] = None
    transferred = False
    try:
        if before_temporary_open_hook is not None:
            before_temporary_open_hook(temporary)
        try:
            target_handle = temporary.open("x+b")
        except FileExistsError as exc:
            raise WorkflowError("stable copy staging path already exists") from exc
        try:
            opened_target = os.fstat(target_handle.fileno())
            if not stat.S_ISREG(opened_target.st_mode) or opened_target.st_nlink != 1:
                raise WorkflowError("staged copy is not one owned regular file")
            # Capture ownership before opening the source.  If the source open
            # itself fails, the finally path can still reclaim this exact
            # create-only staging file without touching a replacement.
            temporary_identity = _owned_node_identity(opened_target)
        except Exception:
            target_handle.close()
            raise
        source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_descriptor = os.open(source, source_flags)
        except OSError as exc:
            target_handle.close()
            raise WorkflowError("stable copy source could not be opened") from exc
        with os.fdopen(source_descriptor, "rb") as source_handle, target_handle:
            opened_source = os.fstat(source_handle.fileno())
            if (
                not stat.S_ISREG(opened_source.st_mode)
                or opened_source.st_nlink != 1
                or list(_owned_node_identity(opened_source)) != before_snapshot["identity"]
                or int(opened_source.st_size) != int(before["length"])
                or int(opened_source.st_mtime_ns) != int(before["last_write_ns"])
            ):
                raise WorkflowError("stable copy source identity changed before descriptor copy")
            digest = hashlib.sha256()
            copied_length = 0
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                target_handle.write(chunk)
                digest.update(chunk)
                copied_length += len(chunk)
                if after_copy_chunk_hook is not None:
                    after_copy_chunk_hook(temporary, copied_length)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            source_handle.seek(0)
            verification_digest = hashlib.sha256()
            verification_length = 0
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                verification_digest.update(chunk)
                verification_length += len(chunk)
            after_source = os.fstat(source_handle.fileno())
            after_target = os.fstat(target_handle.fileno())
            if (
                _owned_node_identity(after_source) != _owned_node_identity(opened_source)
                or after_source.st_nlink != 1
                or int(after_source.st_size) != int(before["length"])
                or int(after_source.st_mtime_ns) != int(before["last_write_ns"])
                or copied_length != int(before["length"])
                or verification_length != copied_length
                or digest.hexdigest() != str(before["sha256"]).removeprefix("sha256:")
                or verification_digest.digest() != digest.digest()
                or _owned_node_identity(after_target) != temporary_identity
                or after_target.st_nlink != 1
                or int(after_target.st_size) != copied_length
            ):
                raise WorkflowError("descriptor copy source or staging identity changed")
        os.utime(
            temporary,
            ns=(int(before["last_write_ns"]), int(before["last_write_ns"])),
        )
        temporary_metadata = os.lstat(temporary)
        if (
            _owned_node_identity(temporary_metadata) != temporary_identity
            or not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_nlink != 1
        ):
            raise WorkflowError("staged copy ownership changed after descriptor close")
        if before_commit_hook is not None:
            before_commit_hook()
        after = file_state(source)
        if not states_equal(before, after):
            raise WorkflowError("copy source changed before commit")
        copied = file_state(temporary)
        if not states_equal(before, copied):
            raise WorkflowError("staged copy does not preserve content and timestamp")
        staged_owner = capture_transaction_node(temporary)
        _rename_create_only(temporary, target, "stable file copy")
        transferred = True
        if published_owner_hook is not None:
            published_owner_hook(staged_owner)
        if after_publish_hook is not None:
            after_publish_hook(target, temporary)
        if not transaction_node_matches(target, staged_owner):
            raise WorkflowError("committed copy ownership changed after atomic publication")
        committed = file_state(target)
        if not states_equal(before, committed):
            raise WorkflowError("committed copy does not match source evidence")
        return committed
    finally:
        if not transferred:
            _unlink_owned_file(temporary, temporary_identity)


def copy_tree_exact(
    source: pathlib.Path,
    target: pathlib.Path,
    expected: Mapping[str, Any],
    *,
    before_commit_hook: Optional[Callable[[], None]] = None,
    before_staging_create_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_file_hook: Optional[Callable[[pathlib.Path, int], None]] = None,
    published_owner_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    after_publish_hook: Optional[Callable[[pathlib.Path, pathlib.Path], None]] = None,
) -> None:
    source = normalized(source)
    target = normalized(target)
    if not expected.get("exists"):
        raise WorkflowError("cannot copy an absent tree")
    if target.exists() or os.path.lexists(target):
        raise WorkflowError("tree copy target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".t-{uuid.uuid4().hex[:16]}.tmp"
    if staging.exists() or os.path.lexists(staging):
        raise WorkflowError("tree staging target already exists")
    if before_staging_create_hook is not None:
        before_staging_create_hook(staging)
    staging.mkdir()
    staging_metadata = os.lstat(staging)
    if is_reparse(staging) or not stat.S_ISDIR(staging_metadata.st_mode):
        raise WorkflowError("tree staging root is unsafe")
    staging_identity = _owned_node_identity(staging_metadata)
    owned_files: list[tuple[pathlib.Path, tuple[int, int, int]]] = []
    owned_directories: list[tuple[pathlib.Path, tuple[int, int, int]]] = []
    staged_owner: Optional[Mapping[str, Any]] = None
    transferred = False
    try:
        entries = list(expected.get("entries") or [])
        directories = [item for item in entries if item.get("kind") == "directory"]
        files = [item for item in entries if item.get("kind") == "file"]
        for entry in directories:
            relative = validate_relative_path(str(entry["relative"]), "tree directory")
            directory = staging / pathlib.Path(*relative.parts)
            directory.mkdir(parents=True, exist_ok=False)
            metadata = os.lstat(directory)
            if is_reparse(directory) or not stat.S_ISDIR(metadata.st_mode):
                raise WorkflowError("tree staging directory is unsafe")
            owned_directories.append((directory, _owned_node_identity(metadata)))
        for file_index, entry in enumerate(files, start=1):
            relative = validate_relative_path(str(entry["relative"]), "tree file")
            staged_file = staging / pathlib.Path(*relative.parts)
            holder: list[Mapping[str, Any]] = []
            copy_file_stable(
                source / pathlib.Path(*relative.parts),
                staged_file,
                published_owner_hook=lambda owner, holder=holder: holder.append(dict(owner)),
            )
            if len(holder) != 1:
                raise WorkflowError("tree staging file ownership was not captured")
            identity_value = holder[0].get("identity")
            if not isinstance(identity_value, list) or len(identity_value) != 3:
                raise WorkflowError("tree staging file identity is invalid")
            owned_files.append((staged_file, tuple(int(item) for item in identity_value)))
            if after_file_hook is not None:
                after_file_hook(staged_file, file_index)
        for entry in sorted(
            directories,
            key=lambda item: len(pathlib.PurePosixPath(str(item["relative"])).parts),
            reverse=True,
        ):
            relative = validate_relative_path(str(entry["relative"]), "tree directory")
            os.utime(
                staging / pathlib.Path(*relative.parts),
                ns=(int(entry["last_write_ns"]), int(entry["last_write_ns"])),
            )
        os.utime(
            staging,
            ns=(int(expected["root_last_write_ns"]), int(expected["root_last_write_ns"])),
        )
        verify_tree(source, expected, "source")
        verify_tree(staging, expected, "staged tree")
        staged_owner = capture_transaction_node(staging)
        if before_commit_hook is not None:
            before_commit_hook()
        _rename_create_only(staging, target, "exact tree copy")
        transferred = True
        if published_owner_hook is not None:
            published_owner_hook(staged_owner)
        if after_publish_hook is not None:
            after_publish_hook(target, staging)
        if not transaction_node_matches(target, staged_owner):
            raise WorkflowError("committed tree ownership changed after atomic publication")
        verify_tree(target, expected, "committed tree")
    finally:
        if not transferred:
            if staged_owner is not None:
                _remove_owned_tree(staging, staged_owner)
            elif os.path.lexists(staging):
                cleanup_failures: list[str] = []
                for path, identity in reversed(owned_files):
                    try:
                        _unlink_owned_file(path, identity)
                    except WorkflowError:
                        cleanup_failures.append(str(path))
                for path, identity in sorted(
                    owned_directories,
                    key=lambda item: len(item[0].parts),
                    reverse=True,
                ):
                    try:
                        claim = _claim_owned_node(path, identity, kind="directory")
                        claim.rmdir()
                    except (OSError, WorkflowError):
                        cleanup_failures.append(str(path))
                try:
                    claim = _claim_owned_node(staging, staging_identity, kind="directory")
                    claim.rmdir()
                except (OSError, WorkflowError):
                    cleanup_failures.append(str(staging))
                if cleanup_failures:
                    raise WorkflowError("tree staging cleanup outcome_unknown; recovery retained")


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise WorkflowError(f"{label} is not a canonical SHA-256 identifier")
    return value


def contract_hash_without(document: Mapping[str, Any], field: str, label: str) -> str:
    payload = copy.deepcopy(dict(document))
    payload.pop(field, None)
    try:
        return contract_canonical_hash(payload)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError(f"{label} is not canonically hashable") from exc


def validate_process_fixture(path: pathlib.Path, test_root: pathlib.Path) -> dict[str, int]:
    path = normalized(path)
    if not path_within(path, test_root):
        raise WorkflowError("process fixture escaped TestRoot")
    assert_no_reparse(path, test_root)
    value = load_json(path, "process fixture")
    if value.get("schema") != "oni.process-state-fixture.v1":
        raise WorkflowError("process fixture schema is invalid")
    counts: dict[str, int] = {}
    for key in ("oxygen_not_included_count", "bridge_count"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise WorkflowError("process fixture contains an invalid count")
        counts[key] = item
    return counts


def split_windows_command_line(value: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError("python process command line is unavailable")
    if os.name != "nt":
        try:
            return [item.strip('"') for item in shlex.split(value, posix=False)]
        except ValueError as exc:
            raise WorkflowError("python process command line is invalid") from exc
    argc = ctypes.c_int(0)
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(value, ctypes.byref(argc))
    if not argv:
        raise WorkflowError("python process command line is invalid")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(argv, ctypes.c_void_p))


def is_oni_bridge_python_command(command_line: str) -> bool:
    arguments = split_windows_command_line(command_line)
    consumes_next = False
    option_value_flags = {"-w", "-x", "--check-hash-based-pycs"}
    for argument in arguments[1:]:
        if consumes_next:
            consumes_next = False
            continue
        folded = argument.casefold()
        if folded in {"-c", "-m"}:
            return False
        if folded in option_value_flags:
            consumes_next = True
            continue
        if folded == "--":
            continue
        if folded == "-":
            return False
        if folded.startswith("-"):
            continue
        return pathlib.PureWindowsPath(argument).name.casefold() == "oni_bridge.py"
    return False


def count_bridge_process_records(records: Any) -> int:
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        raise WorkflowError("live process guard returned an invalid python process list")
    allowed_names = {"python.exe", "pythonw.exe", "py.exe", "python", "pythonw", "py"}
    count = 0
    for record in records:
        if not isinstance(record, dict):
            raise WorkflowError("live process guard returned an invalid python process record")
        name = str(record.get("Name") or record.get("name") or "").casefold()
        if name not in allowed_names:
            continue
        command_line = record.get("CommandLine") if "CommandLine" in record else record.get("command_line")
        if not isinstance(command_line, str):
            raise WorkflowError("python process command line is unavailable")
        if is_oni_bridge_python_command(command_line):
            count += 1
    return count


def live_process_counts() -> dict[str, int]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise WorkflowError("PowerShell is unavailable for the live process guard")
    command = (
        "$g=@(Get-Process -Name 'OxygenNotIncluded' -ErrorAction SilentlyContinue).Count;"
        "$n=@('python.exe','pythonw.exe','py.exe');"
        "$p=@(Get-CimInstance Win32_Process -ErrorAction Stop|"
        "Where-Object{$n -contains $_.Name}|Select-Object Name,CommandLine);"
        "@{oxygen_not_included_count=$g;python_processes=@($p)}|ConvertTo-Json -Depth 4 -Compress"
    )
    completed = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkflowError("live process guard failed closed")
    try:
        value = json.loads(completed.stdout)
        return {
            "oxygen_not_included_count": int(value["oxygen_not_included_count"]),
            "bridge_count": count_bridge_process_records(value.get("python_processes", [])),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowError("live process guard returned invalid evidence") from exc


def process_counts(args: argparse.Namespace, roots: "Roots") -> dict[str, int]:
    if roots.test_root is not None:
        if not args.process_state_fixture:
            raise WorkflowError("TestRoot mode requires a process-state fixture")
        return validate_process_fixture(pathlib.Path(args.process_state_fixture), roots.test_root)
    if args.process_state_fixture:
        raise WorkflowError("process fixtures are forbidden in live mode")
    return live_process_counts()


def require_processes_stopped(counts: Mapping[str, int]) -> None:
    if int(counts.get("oxygen_not_included_count", -1)) != 0:
        raise WorkflowError("Oxygen Not Included must be stopped")
    if int(counts.get("bridge_count", -1)) != 0:
        raise WorkflowError("oni_bridge.py must be stopped")


def bridge_gate(bridge: pathlib.Path, *, require_bridge: bool) -> dict[str, Any]:
    bridge = normalized(bridge)
    if not bridge.exists():
        if require_bridge:
            raise WorkflowError("runtime bridge is missing")
        return {
            "bridge_exists": False,
            "policy_disabled": True,
            "nonterminal": {name: 0 for name in NONTERMINAL_QUEUES},
            "temporary_files": 0,
        }
    if not bridge.is_dir() or is_reparse(bridge):
        raise WorkflowError("bridge root must be a non-reparse directory")
    policy_path = bridge / "action-policy.json"
    policy = load_json(policy_path, "action policy")
    if (
        policy.get("schema") != "oni.action-policy.v1"
        or policy.get("actions_enabled") is not False
        or policy.get("suggestions_enabled") is not False
        or policy.get("allowed_save_hashes") != []
    ):
        raise WorkflowError("action policy must be disabled, suggestion-free, and unbound")
    counts: dict[str, int] = {}
    commands = bridge / "commands"
    temporary = 0
    if commands.exists() and (not commands.is_dir() or is_reparse(commands)):
        raise WorkflowError("command queue root is unsafe")
    for stage in NONTERMINAL_QUEUES:
        directory = commands / stage
        if directory.exists():
            if not directory.is_dir() or is_reparse(directory):
                raise WorkflowError("command queue path is unsafe")
            entries = list(directory.iterdir())
            if any(is_reparse(entry) for entry in entries):
                raise WorkflowError("command queue entry is a reparse point")
            # A nonterminal queue is empty only when it has no entries at all;
            # counting JSON alone would silently admit an unknown partial file.
            counts[stage] = len(entries)
        else:
            counts[stage] = 0
    if commands.exists():
        for candidate in commands.rglob("*"):
            if is_reparse(candidate):
                raise WorkflowError("command tree contains a reparse point")
            if candidate.is_file() and candidate.name.lower().endswith(".tmp"):
                temporary += 1
    if any(counts.values()) or temporary:
        raise WorkflowError("nonterminal or temporary command queues are not empty")
    return {
        "bridge_exists": True,
        "policy_disabled": True,
        "nonterminal": counts,
        "temporary_files": 0,
        "policy_sha256": sha256_file(policy_path),
    }


def _decode_pipeline_json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                WorkflowError(f"non-finite JSON constant: {item}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"{label} is not valid strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} root must be an object")
    return dict(value)


def _stable_pipeline_json_snapshot(path: pathlib.Path, label: str) -> dict[str, Any]:
    snapshot = _stable_file_snapshot(
        path,
        max_bytes=MAX_JSON_BYTES,
        collect_bytes=True,
    )
    raw = snapshot.get("bytes")
    if not isinstance(raw, bytes):
        raise WorkflowError(f"{label} has no stable JSON payload")
    document = _decode_pipeline_json_object(raw, label)
    try:
        document_hash = contract_canonical_hash(document)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError(f"{label} is not canonically hashable") from exc
    return {
        "document": document,
        "file_sha256": snapshot["sha256"],
        "document_hash": document_hash,
        "identity": list(snapshot["identity"]),
        "length": int(snapshot["length"]),
        "last_write_ns": int(snapshot["last_write_ns"]),
    }


def _confirm_pipeline_json_snapshot(
    path: pathlib.Path,
    label: str,
    expected: Mapping[str, Any],
) -> None:
    if _stable_pipeline_json_snapshot(path, label) != dict(expected):
        raise WorkflowError(f"{label} changed across command pipeline validation")


def _pipeline_state_log_path_set(bridge: pathlib.Path) -> tuple[str, ...]:
    """Freeze every state-log-like root entry, including unknown suffixes."""

    paths: list[str] = []
    for candidate in sorted(
        bridge.glob("state.jsonl*"),
        key=lambda item: item.name.casefold(),
    ):
        if not os.path.lexists(candidate):
            continue
        if is_reparse(candidate) or not candidate.is_file():
            raise WorkflowError("Bridge state log path set contains an unsafe entry")
        relative = validate_relative_path(candidate.relative_to(bridge).as_posix(), "Bridge state log")
        paths.append(relative.as_posix())
    return tuple(paths)


def _shutdown_raw_final_tip(
    bridge: pathlib.Path,
    shutdown_document: Mapping[str, Any],
) -> tuple[dict[str, Any], pathlib.Path, dict[str, Any]]:
    """Read the shutdown-authenticated raw Probe tip through one descriptor."""

    source = (
        shutdown_document.get("source")
        if isinstance(shutdown_document.get("source"), Mapping)
        else {}
    )
    termination = (
        shutdown_document.get("termination")
        if isinstance(shutdown_document.get("termination"), Mapping)
        else {}
    )
    final_tip_projection = (
        termination.get("final_tip_snapshot")
        if isinstance(termination.get("final_tip_snapshot"), Mapping)
        else {}
    )
    final_tip = (
        shutdown_document.get("final_tip")
        if isinstance(shutdown_document.get("final_tip"), Mapping)
        else {}
    )
    source_path = normalized(pathlib.Path(str(source.get("path") or "")))
    if not path_within(source_path, bridge):
        raise WorkflowError("shutdown final tip source escaped the runtime bridge")
    source_snapshot = _stable_file_snapshot(
        source_path,
        max_bytes=shutdown_contract.MAX_LOG_BYTES,
        collect_bytes=True,
    )
    payload = source_snapshot.get("bytes")
    if not isinstance(payload, bytes):
        raise WorkflowError("shutdown final tip source has no stable payload")
    if (
        source_snapshot.get("sha256") != source.get("file_sha256")
        or source_snapshot.get("length") != source.get("byte_length")
        or (payload and not payload.endswith(b"\n"))
    ):
        raise WorkflowError("shutdown final tip source lost its exact evidence binding")
    lines = payload.splitlines(keepends=True)
    line_number = final_tip_projection.get("line_number")
    if (
        isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or line_number < 1
        or line_number > len(lines)
        or source.get("complete_line_count") != len(lines)
    ):
        raise WorkflowError("shutdown final tip line projection is invalid")
    raw_state = _decode_pipeline_json_object(
        lines[line_number - 1],
        "shutdown final raw snapshot",
    )
    if (
        "bridge" in raw_state
        or raw_state.get("schema") != "oni.state.v2"
        or raw_state.get("event") != "state_snapshot"
        or raw_state.get("session_id") != final_tip.get("session_id")
        or raw_state.get("snapshot_id") != final_tip.get("snapshot_id")
        or raw_state.get("save_id_hash") != final_tip.get("save_id_hash")
        or raw_state.get("seq") != final_tip_projection.get("seq")
        or raw_state.get("snapshot_id") != final_tip_projection.get("snapshot_id")
        or raw_state.get("game_build") != final_tip_projection.get("game_build")
        or raw_state.get("mod_version") != final_tip_projection.get("mod_version")
        or contract_canonical_hash(raw_state) != final_tip_projection.get("record_hash")
        or canonical_state_hash(raw_state) != final_tip.get("state_hash")
    ):
        raise WorkflowError("shutdown final raw snapshot does not match the trusted final tip")
    return raw_state, source_path, {
        "identity": list(source_snapshot["identity"]),
        "length": int(source_snapshot["length"]),
        "last_write_ns": int(source_snapshot["last_write_ns"]),
        "sha256": str(source_snapshot["sha256"]),
    }


def _expected_pipeline_startup_expectations(
    expected_run_id: str,
    runtime_config: "RuntimeConfigPreflight",
    certificate: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild Bridge replay inputs from independently authenticated anchors."""

    binding = certificate.get("binding") if isinstance(certificate.get("binding"), Mapping) else {}
    process = certificate.get("process") if isinstance(certificate.get("process"), Mapping) else {}
    session = certificate.get("session") if isinstance(certificate.get("session"), Mapping) else {}
    loader = certificate.get("loader") if isinstance(certificate.get("loader"), Mapping) else {}
    startup_intent = (
        runtime_config.binding.get("startup_intent")
        if isinstance(runtime_config.binding.get("startup_intent"), Mapping)
        else {}
    )
    expected = {
        "validation_run_id": expected_run_id,
        "validation_process_id": process.get("process_id"),
        "startup_binding_hash": runtime_config.startup_binding_file_sha256,
        "startup_intent_hash": startup_intent.get("document_hash"),
        "runtime_config_evidence_sha256": runtime_config.file_sha256,
        "loader_path_id_upper": startup_intent.get("requested_loader_path_id_upper"),
        "loader_save_sha256": startup_intent.get("requested_loader_save_sha256"),
        "game_build": startup_intent.get("expected_game_build"),
        "mod_version": startup_intent.get("expected_mod_version"),
    }
    if (
        certificate.get("validation_run_id") != expected_run_id
        or binding.get("startup_binding_sha256") != expected["startup_binding_hash"]
        or binding.get("startup_intent_hash") != expected["startup_intent_hash"]
        or binding.get("runtime_config_evidence_sha256")
        != expected["runtime_config_evidence_sha256"]
        or binding.get("requested_save_path_id") != expected["loader_path_id_upper"]
        or session.get("initial_save_sha256") != expected["loader_save_sha256"]
        or loader.get("expected_game_build") != expected["game_build"]
        or loader.get("expected_mod_version") != expected["mod_version"]
    ):
        raise WorkflowError("pipeline startup anchors do not match runtime configuration")
    try:
        validated = priority_pair_contract.oni_bridge.validate_startup_expectations(expected)
    except ValueError as exc:
        raise WorkflowError("pipeline startup expectations are invalid") from exc
    if validated is None or dict(validated) != expected:
        raise WorkflowError("pipeline startup expectations did not validate exactly")
    return expected


def command_pipeline_gate(
    bridge: pathlib.Path,
    *,
    expected_save_id_hash: str,
    frozen_pipeline: Mapping[str, Any],
    expected_final_tip: Mapping[str, Any],
    shutdown_document: Mapping[str, Any],
    expected_run_id: str,
    runtime_config: "RuntimeConfigPreflight",
    startup_certificate: Mapping[str, Any],
    before_final_bridge_tree_verify_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild the pair-time terminal pipeline proof after normal shutdown.

    Wall-clock age is frozen only at pair publication.  Every byte-bearing and
    structural input (raw/enriched tip, health, logs, terminal archive,
    companions, policy, and command tree) is revalidated against current
    descriptor-stable custody without granting new mutation authority.
    """

    frozen_inputs = (
        frozen_pipeline.get("inputs")
        if isinstance(frozen_pipeline.get("inputs"), Mapping)
        else {}
    )
    frozen_freshness = (
        frozen_inputs.get("snapshot_freshness")
        if isinstance(frozen_inputs.get("snapshot_freshness"), Mapping)
        else {}
    )
    if (
        set(frozen_pipeline) != PAIR_PIPELINE_KEYS
        or set(frozen_inputs) != PAIR_PIPELINE_INPUT_KEYS
        or set(frozen_freshness) != PAIR_PIPELINE_FRESHNESS_KEYS
    ):
        raise WorkflowError("frozen pair command pipeline shape is invalid")

    bridge_tree_guard = snapshot_tree(bridge)
    commands_root = normalized(bridge / "commands")
    for name in COMMAND_DIRS:
        directory = commands_root / name
        if not directory.is_dir() or is_reparse(directory):
            raise WorkflowError("command pipeline directory set is incomplete or unsafe")
    commands_tree_guard = snapshot_tree(commands_root)
    latest_state_path = normalized(bridge / "latest_state.json")
    health_path = normalized(bridge / "health.json")
    policy_path = normalized(bridge / "action-policy.json")
    for path in (latest_state_path, health_path, policy_path):
        assert_no_reparse(path, bridge)
    latest_snapshot = _stable_pipeline_json_snapshot(latest_state_path, "latest state")
    health_snapshot = _stable_pipeline_json_snapshot(health_path, "Bridge health")
    policy_snapshot = _stable_pipeline_json_snapshot(policy_path, "action policy")
    state_log_paths_before = _pipeline_state_log_path_set(bridge)
    latest_state = latest_snapshot["document"]
    health = health_snapshot["document"]
    policy = policy_snapshot["document"]
    raw_state, source_path, source_snapshot = _shutdown_raw_final_tip(
        bridge,
        shutdown_document,
    )
    if raw_state.get("save_id_hash") != require_hash(
        expected_save_id_hash,
        "expected pipeline save hash",
    ):
        raise WorkflowError("command pipeline raw tip does not bind the final lineage save")

    try:
        publication = priority_pair_contract.validate_enriched_latest_against_raw(
            raw_state,
            latest_state,
            health,
        )
    except priority_pair_contract.PriorityPairError as exc:
        raise WorkflowError(f"terminal Bridge publication is invalid: {exc.code}") from exc
    if (
        publication.get("terminal") is not True
        or publication.get("core_state_hash") != expected_final_tip.get("state_hash")
        or raw_state.get("session_id") != expected_final_tip.get("session_id")
        or raw_state.get("snapshot_id") != expected_final_tip.get("snapshot_id")
        or raw_state.get("save_id_hash") != expected_final_tip.get("save_id_hash")
    ):
        raise WorkflowError("command pipeline publication does not bind the frozen pair tip")

    checked_at = _parse_aware_timestamp(
        frozen_freshness.get("checked_at"),
        "pair-time command pipeline freshness",
    )
    health_updated_at = _parse_aware_timestamp(
        publication.get("health_updated_at"),
        "terminal Bridge health publication",
    )
    if health_updated_at > checked_at:
        raise WorkflowError("terminal Bridge health was published after the pair checkpoint")
    frozen_age, frozen_threshold, frozen_blockers = snapshot_freshness(
        latest_state,
        now=checked_at,
    )
    structural_blockers = [item for item in frozen_blockers if item != "snapshot_stale"]
    sample = latest_state.get("sample")
    sample_interval = sample.get("interval_seconds") if isinstance(sample, Mapping) else None
    expected_freshness = {
        "checked_at": checked_at.isoformat(timespec="microseconds"),
        "snapshot_ts": latest_state.get("ts"),
        "sample_interval_seconds": sample_interval,
        "age_seconds": frozen_age,
        "threshold_seconds": MAX_PIPELINE_STATE_AGE_SECONDS,
        "blockers": structural_blockers,
    }
    if (
        dict(frozen_freshness) != expected_freshness
        or frozen_age is None
        or frozen_threshold is None
        or structural_blockers != []
        or frozen_age > MAX_PIPELINE_STATE_AGE_SECONDS
    ):
        raise WorkflowError("frozen pair command pipeline was not fresh at creation")

    try:
        pipeline = command_pipeline_readiness(
            bridge,
            latest_state=latest_state,
            policy=policy,
        )
        readiness = pipeline.get("readiness") if isinstance(pipeline.get("readiness"), Mapping) else {}
        proposal = (
            readiness.get("mutation_proposal")
            if isinstance(readiness.get("mutation_proposal"), Mapping)
            else {}
        )
        confirmation = (
            readiness.get("mutation_confirmation")
            if isinstance(readiness.get("mutation_confirmation"), Mapping)
            else {}
        )
        context = (
            pipeline.get("evaluation_context")
            if isinstance(pipeline.get("evaluation_context"), Mapping)
            else {}
        )
        if (
            set(pipeline)
            != {
                "schema",
                "evaluation_context",
                "ready",
                "blockers",
                "readiness",
                "counts",
                "commands",
                "pending_candidates",
                "in_flight_mutations",
                "ambiguous_outcomes",
                "duplicate_ids",
                "stale_pending",
                "invalid_records",
            }
            or pipeline.get("schema") != COMMAND_PIPELINE_SCHEMA
            # Evaluated without a proposal frame, as called above.
            or context.get("phase") != "pre_frame"
            or context.get("proposal_frame_id") is not None
            or pipeline.get("pending_candidates") != []
            or pipeline.get("ready") is not True
            or pipeline.get("blockers") != []
            or proposal.get("ready") is not True
            or proposal.get("blockers") != []
            or confirmation.get("ready") is not True
            or confirmation.get("blockers") != []
            or pipeline.get("commands") != []
            or pipeline.get("in_flight_mutations") != []
            or pipeline.get("ambiguous_outcomes") != []
            or pipeline.get("duplicate_ids") != []
            or pipeline.get("stale_pending") != []
            or pipeline.get("invalid_records") != []
        ):
            raise WorkflowError("command pipeline has an unresolved readiness latch")
        counts = pipeline.get("counts") if isinstance(pipeline.get("counts"), Mapping) else {}
        if not counts or any(
            isinstance(value, bool) or not isinstance(value, int) or value != 0
            for value in counts.values()
        ):
            raise WorkflowError("command pipeline counts are not exactly empty")
        published_at = health_updated_at
        terminal_archive, terminal_archive_snapshot = (
            priority_pair_contract._terminal_archive_evidence(bridge, raw_state)
        )
        replay, replay_snapshots = priority_pair_contract._bounded_bridge_replay(
            bridge,
            raw_state,
            health,
            published_at=published_at,
        )
        companion_outputs, companion_snapshots = (
            priority_pair_contract._bridge_companion_evidence(
                bridge,
                raw_state,
                latest_state,
                health,
                pipeline,
                policy,
            )
        )
        command_tree_before = priority_pair_contract._command_tree_snapshot(bridge)
    except priority_pair_contract.PriorityPairError as exc:
        raise WorkflowError(f"frozen pair runtime custody is invalid: {exc.code}") from exc
    except (OSError, UnicodeError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("frozen pair runtime custody could not be rebuilt") from exc

    expected_startup = _expected_pipeline_startup_expectations(
        expected_run_id,
        runtime_config,
        startup_certificate,
    )
    if replay.get("startup_expectations_hash") != contract_canonical_hash(expected_startup):
        raise WorkflowError("Bridge replay startup expectations were not independently anchored")
    replay_state_logs = replay.get("state_logs") if isinstance(replay.get("state_logs"), list) else []
    replay_log_paths = tuple(
        sorted(
            str(entry.get("path") or "")
            for entry in replay_state_logs
            if isinstance(entry, Mapping)
        )
    )
    if (
        len(replay_log_paths) != len(replay_state_logs)
        or replay_log_paths != tuple(sorted(state_log_paths_before))
    ):
        raise WorkflowError("Bridge replay did not bind the complete state log path set")
    try:
        pipeline_hash = contract_canonical_hash(pipeline)
        policy_hash = contract_canonical_hash(policy)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("command pipeline readiness is not canonically hashable") from exc
    current_inputs = {
        "latest_state_file_sha256": latest_snapshot["file_sha256"],
        "latest_state_hash": publication["enriched_state_hash"],
        "latest_state_core_hash": publication["core_state_hash"],
        "health_file_sha256": health_snapshot["file_sha256"],
        "health_hash": health_snapshot["document_hash"],
        "health_updated_at": publication["health_updated_at"],
        "health_status": publication["health_status"],
        "terminal_publication": publication["terminal"],
        "bridge_projection": copy.deepcopy(publication["bridge"]),
        "startup_binding": copy.deepcopy(health["startup_binding"]),
        "policy_file_sha256": policy_snapshot["file_sha256"],
        "policy_hash": policy_hash,
        "terminal_archive": terminal_archive,
        "bridge_replay": replay,
        "companion_outputs": companion_outputs,
        "command_tree_hash": command_tree_before["tree_hash"],
        "command_tree_entry_count": command_tree_before["entry_count"],
        "snapshot_freshness": expected_freshness,
    }
    expected_pipeline = {
        "schema": pipeline.get("schema"),
        "gate_hash": pipeline_hash,
        "inputs": current_inputs,
        "mutation_proposal": copy.deepcopy(dict(proposal)),
        "counts": copy.deepcopy(dict(counts)),
        "command_count": 0,
        "ambiguous_outcome_count": 0,
        "duplicate_id_count": 0,
        "stale_pending_count": 0,
        "invalid_record_count": 0,
    }
    try:
        frozen_pipeline_hash = contract_canonical_hash(dict(frozen_pipeline))
        expected_pipeline_hash = contract_canonical_hash(expected_pipeline)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("frozen pair command pipeline is not canonically hashable") from exc
    if frozen_pipeline_hash != expected_pipeline_hash:
        raise WorkflowError("frozen pair command pipeline does not match current exact inputs")

    for name, snapshot in companion_snapshots.items():
        priority_pair_contract._confirm_object_snapshot(bridge / name, name, snapshot)
    for path_text, snapshot in replay_snapshots.items():
        priority_pair_contract._confirm_binary_snapshot(
            pathlib.Path(path_text),
            f"Bridge state log:{pathlib.Path(path_text).name}",
            snapshot,
        )
    priority_pair_contract._confirm_binary_snapshot(
        terminal_archive_snapshot["path"],
        "terminal session archive",
        terminal_archive_snapshot,
    )
    _confirm_pipeline_json_snapshot(latest_state_path, "latest state", latest_snapshot)
    _confirm_pipeline_json_snapshot(health_path, "Bridge health", health_snapshot)
    _confirm_pipeline_json_snapshot(policy_path, "action policy", policy_snapshot)
    source_after = _stable_file_snapshot(
        source_path,
        max_bytes=shutdown_contract.MAX_LOG_BYTES,
    )
    if {
        "identity": list(source_after["identity"]),
        "length": int(source_after["length"]),
        "last_write_ns": int(source_after["last_write_ns"]),
        "sha256": str(source_after["sha256"]),
    } != source_snapshot:
        raise WorkflowError("shutdown final tip source changed across pipeline validation")
    command_tree_after = priority_pair_contract._command_tree_snapshot(bridge)
    if command_tree_after != command_tree_before:
        raise WorkflowError("command tree changed across pipeline validation")
    if before_final_bridge_tree_verify_hook is not None:
        before_final_bridge_tree_verify_hook(bridge)
    if _pipeline_state_log_path_set(bridge) != state_log_paths_before:
        raise WorkflowError("Bridge state log path set changed across pipeline validation")
    verify_tree(commands_root, commands_tree_guard, "command pipeline")
    verify_tree(bridge, bridge_tree_guard, "terminal Bridge pipeline")
    return {
        "schema": COMMAND_PIPELINE_SCHEMA,
        "ready": True,
        "document_hash": pipeline_hash,
        "latest_state_sha256": latest_snapshot["file_sha256"],
        "latest_state_path_id_lower": path_id(latest_state_path, contract="lower"),
        "health_sha256": health_snapshot["file_sha256"],
        "health_path_id_lower": path_id(health_path, contract="lower"),
        "pair_creation_freshness": copy.deepcopy(dict(frozen_freshness)),
        "frozen_pipeline_hash": contract_canonical_hash(dict(frozen_pipeline)),
        "ambiguous_outcome_count": 0,
        "duplicate_id_count": 0,
        "stale_pending_count": 0,
        "invalid_record_count": 0,
    }, bridge_tree_guard


@dataclass(frozen=True)
class Roots:
    run_id: str
    oni_root: pathlib.Path
    evidence_root: pathlib.Path
    boundary_report_root: pathlib.Path
    test_root: Optional[pathlib.Path]
    live_mode: bool

    @property
    def environment_dir(self) -> pathlib.Path:
        return self.evidence_root / self.run_id / "environment"


@dataclass(frozen=True)
class RuntimeConfigPreflight:
    path: pathlib.Path
    file_sha256: str
    document: dict[str, Any]
    binding: dict[str, Any]
    binding_hash: str
    configured_at: dt.datetime
    verification: Optional[dict[str, Any]]
    startup_binding_path: pathlib.Path
    startup_binding_file_sha256: str


@dataclass(frozen=True)
class StartupConsumptionPreflight:
    path: pathlib.Path
    relative: str
    evidence_hash: str
    validation_process_id: int
    certificate: dict[str, Any]
    certificate_hash: str
    custody: dict[str, Any]
    custody_hash: str


def resolve_roots(args: argparse.Namespace) -> Roots:
    if RUN_ID_RE.fullmatch(args.run_id or "") is None:
        raise WorkflowError("run id is invalid")
    test_root = assert_safe_root(pathlib.Path(args.test_root), "TestRoot") if args.test_root else None
    oni_root = assert_safe_root(pathlib.Path(args.oni_root), "ONI root")
    evidence_root = assert_safe_root(pathlib.Path(args.evidence_root), "evidence root")
    boundary_root = assert_safe_root(pathlib.Path(args.boundary_report_root), "boundary report root")
    live_default = normalized(
        pathlib.Path(os.environ.get("USERPROFILE", str(pathlib.Path.home())))
        / "Documents"
        / "Klei"
        / "OxygenNotIncluded"
    )
    if test_root is not None:
        if not path_within(oni_root, test_root) or not path_within(evidence_root, test_root):
            raise WorkflowError("fixture ONI/evidence roots must be strict descendants of TestRoot")
        if not path_within(boundary_root, evidence_root, strict=False):
            raise WorkflowError("fixture boundary reports must stay inside the evidence root")
        if paths_overlap(test_root, live_default) or paths_overlap(test_root, PROJECT_ROOT):
            raise WorkflowError("TestRoot cannot overlap live ONI data or the workspace")
    else:
        if not same_path(oni_root, live_default):
            raise WorkflowError("live mode is restricted to the default ONI user-data root")
        if not path_within(evidence_root, PROJECT_ROOT):
            raise WorkflowError("live evidence must stay under the workspace")
        if not path_within(boundary_root, evidence_root, strict=False):
            raise WorkflowError("live boundary reports must stay under the evidence root")
        if args.apply and args.confirm_live_run_id != args.run_id:
            raise WorkflowError("live apply requires an exact --confirm-live-run-id")
    if paths_overlap(oni_root, evidence_root) or not same_volume(oni_root, evidence_root):
        raise WorkflowError("ONI and evidence roots must be disjoint on the same local volume")
    assert_no_reparse(oni_root, test_root or oni_root)
    assert_no_reparse(evidence_root, test_root or PROJECT_ROOT)
    assert_no_reparse(boundary_root, evidence_root)
    return Roots(
        run_id=args.run_id,
        oni_root=oni_root,
        evidence_root=evidence_root,
        boundary_report_root=boundary_root,
        test_root=test_root,
        live_mode=test_root is None,
    )


def save_root_for(path: pathlib.Path, oni_root: pathlib.Path) -> pathlib.Path:
    for relative in ("save_files", "cloud_save_files"):
        root = oni_root / relative
        if root.exists() and path_within(path, root):
            assert_no_reparse(path, root)
            return root
    raise WorkflowError("save path is outside the allowed ONI save roots")


def validate_clone_inputs(
    roots: Roots,
    pre_boundary_path: pathlib.Path,
    clone_manifest_path: pathlib.Path,
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
    loader_stem: str,
    *,
    require_initial_loader_content: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_windows_segment(loader_stem, "loader stem")
    source_save = normalized(source_save)
    loader_save = normalized(loader_save)
    if source_save.suffix != ".sav" or loader_save.suffix != ".sav":
        raise WorkflowError("source and loader must use the exact .sav extension")
    source_root = save_root_for(source_save, roots.oni_root)
    loader_root = save_root_for(loader_save, roots.oni_root)
    # Review P0-1: the loader clone lives in its own colony folder, named by
    # its stem, beside the source colony folder.  In the source folder ONI's
    # autosave rotation deletes the user's same-GUID autosaves.
    if (
        not same_path(source_root, loader_root)
        or paths_overlap(source_save.parent, loader_save.parent)
        or not same_path(source_save.parent.parent, loader_save.parent.parent)
        or loader_save.parent.name != loader_stem
    ):
        raise WorkflowError("loader must sit in its own colony folder beside the source colony")
    if source_save == loader_save or loader_save.stem != loader_stem:
        raise WorkflowError("loader save identity does not match its declared stem")
    source_preview = source_save.with_suffix(".png")
    loader_preview = loader_save.with_suffix(".png")
    for path in (source_save, source_preview, loader_save, loader_preview):
        if not path.is_file() or is_reparse(path):
            raise WorkflowError("source/loader save pair is missing or unsafe")

    pre = load_json(pre_boundary_path, "pre-boundary report")
    if (
        pre.get("schema") != BOUNDARY_SCHEMA
        or pre.get("phase") != "pre"
        or pre.get("run_id") != roots.run_id
        or pre.get("oni_root_id") != path_id(roots.oni_root, contract="lower")
        or pre.get("path_disclosure") != "identifiers_only"
    ):
        raise WorkflowError("pre-boundary report does not bind this run/root")

    clone = load_json(clone_manifest_path, "clone manifest")
    if clone.get("schema") != CLONE_SCHEMA or clone.get("created") is not True or clone.get("run_id") != roots.run_id:
        raise WorkflowError("clone manifest does not bind this run")
    if clone.get("oni_root_id") != path_id(roots.oni_root, contract="lower"):
        raise WorkflowError("clone manifest ONI root identity is wrong")
    source = clone.get("source") if isinstance(clone.get("source"), dict) else {}
    target = clone.get("target") if isinstance(clone.get("target"), dict) else {}
    source_save_record = source.get("save") if isinstance(source.get("save"), dict) else {}
    source_preview_record = source.get("preview") if isinstance(source.get("preview"), dict) else {}
    expected = {
        "source_save": (source_save_record, source_save),
        "source_preview": (source_preview_record, source_preview),
    }
    for label, (record, path) in expected.items():
        if (
            record.get("path_id") != path_id(path, contract="lower")
            or record.get("sha256") != sha256_file(path)
            or int(record.get("length", -1)) != path.stat().st_size
        ):
            raise WorkflowError(f"clone manifest {label} no longer matches")
    if (
        target.get("isolation") != "own_colony_folder"
        or source.get("colony_directory_path_id") != path_id(source_save.parent, contract="lower")
        or source.get("redirect_colony_directory_path_id")
        in {None, path_id(loader_save.parent, contract="lower")}
        or target.get("save_base_path_id") != path_id(loader_save.parent.parent, contract="lower")
        or (source.get("header") or {}).get("is_auto_save") is not False
    ):
        raise WorkflowError("clone manifest does not describe an isolated non-autosave clone")
    if (
        clone.get("target_stem_id") != text_id(loader_stem)
        or target.get("directory_path_id") != path_id(loader_save.parent, contract="lower")
        or target.get("save_path_id") != path_id(loader_save, contract="lower")
        or target.get("preview_path_id") != path_id(loader_preview, contract="lower")
        or target.get("save_sha256") != source_save_record.get("sha256")
        or target.get("preview_sha256") != source_preview_record.get("sha256")
    ):
        raise WorkflowError("clone manifest target identity/content is wrong")
    if require_initial_loader_content and (
        target.get("save_sha256") != sha256_file(loader_save)
        or target.get("preview_sha256") != sha256_file(loader_preview)
    ):
        raise WorkflowError("loader clone changed before environment prepare")
    pre_save_ids = {
        item.get("path_id")
        for item in (pre.get("saves") or {}).get("entries", [])
        if isinstance(item, dict)
    }
    if path_id(loader_save, contract="lower") in pre_save_ids or path_id(loader_preview, contract="lower") in pre_save_ids:
        raise WorkflowError("loader pair already existed in the pre-boundary report")
    loader_folder_id = path_id(loader_save.parent, contract="lower")
    if any(
        isinstance(item, dict) and item.get("parent_path_id") == loader_folder_id
        for item in (pre.get("saves") or {}).get("entries", [])
    ):
        raise WorkflowError("loader colony folder already held saves in the pre-boundary report")
    return pre, clone


def manifest_config_records(oni_root: pathlib.Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for relative in CONFIG_RELATIVES:
        path = oni_root / pathlib.Path(relative)
        assert_no_reparse(path, oni_root, allow_missing_leaf=True)
        result.append(
            {
                "relative": relative,
                "path_id_lower": path_id(path, contract="lower"),
                "path_id_upper": path_id(path, contract="upper"),
                **optional_file_state(path),
                "backup_relative": f"backup/config/{relative}" if path.exists() else None,
            }
        )
    return result


def append_journal(
    journal_path: pathlib.Path,
    event: str,
    data: Mapping[str, Any],
    *,
    fault_hook: Optional[Callable[[str], None]] = None,
    before_open_hook: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    journal_path = normalized(journal_path)
    entries, prior = _snapshot_journal(journal_path)
    previous = entries[-1]["entry_hash"] if entries else None
    entry: dict[str, Any] = {
        "schema": JOURNAL_SCHEMA,
        "index": len(entries) + 1,
        "recorded_at": now_iso(),
        "event": event,
        "data": dict(data),
        "previous_entry_hash": previous,
    }
    entry["entry_hash"] = sha256_bytes(canonical_bytes(entry))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_bytes(entry) + b"\n"
    if before_open_hook is not None:
        before_open_hook()
    try:
        handle = journal_path.open("x+b" if prior is None else "r+b")
    except FileExistsError as exc:
        raise WorkflowError("a foreign restore journal appeared before create-exclusive append") from exc
    except OSError as exc:
        raise WorkflowError("restore journal could not be opened") from exc
    with handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise WorkflowError("restore journal is not one owned regular file")
        identity = _owned_node_identity(opened)
        original_size = opened.st_size
        path_opened = os.lstat(journal_path)
        if _owned_node_identity(path_opened) != identity or path_opened.st_nlink != 1:
            raise WorkflowError("restore journal path changed before append")
        handle.seek(0)
        opened_bytes = handle.read(original_size + 1)
        if prior is None:
            if original_size != 0 or opened_bytes:
                raise WorkflowError("new restore journal was not created empty")
        elif (
            identity != tuple(prior["identity"])
            or original_size != prior["size"]
            or sha256_bytes(opened_bytes) != prior["sha256"]
        ):
            raise WorkflowError("restore journal changed between verification and append")
        try:
            if fault_hook is not None:
                fault_hook("before_write")
            handle.seek(0, os.SEEK_END)
            if handle.tell() != original_size:
                raise WorkflowError("restore journal size changed before append")
            handle.write(line)
            if fault_hook is not None:
                fault_hook("after_write")
            handle.flush()
            os.fsync(handle.fileno())
            if fault_hook is not None:
                fault_hook("after_fsync")
            after = os.fstat(handle.fileno())
            path_after = os.lstat(journal_path)
            if (
                _owned_node_identity(after) != identity
                or _owned_node_identity(path_after) != identity
                or after.st_size != original_size + len(line)
            ):
                raise WorkflowError("restore journal identity/length changed during append")
        except Exception as exc:
            try:
                path_now = os.lstat(journal_path)
                if _owned_node_identity(path_now) != identity or path_now.st_nlink != 1:
                    raise WorkflowError("restore journal ownership changed; replacement retained")
                handle.seek(original_size)
                handle.truncate(original_size)
                handle.flush()
                os.fsync(handle.fileno())
            except Exception as rollback_exc:
                raise WorkflowError("restore journal append outcome_unknown; truncation failed") from rollback_exc
            raise WorkflowError("restore journal append failed and was truncated") from exc
    return entry


def _snapshot_journal(
    journal_path: pathlib.Path,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    journal_path = normalized(journal_path)
    if not os.path.lexists(journal_path):
        return [], None
    try:
        before = os.lstat(journal_path)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_JSON_BYTES
        ):
            raise WorkflowError("restore journal is unsafe, hardlinked, or too large")
        descriptor = os.open(journal_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _owned_node_identity(opened) != _owned_node_identity(before) or opened.st_nlink != 1:
                raise WorkflowError("restore journal name changed before descriptor open")
            raw_bytes = handle.read(MAX_JSON_BYTES + 1)
            after_handle = os.fstat(handle.fileno())
        after_path = os.lstat(journal_path)
    except OSError as exc:
        raise WorkflowError("restore journal could not be read stably") from exc
    if (
        len(raw_bytes) > MAX_JSON_BYTES
        or _owned_node_identity(after_handle) != _owned_node_identity(opened)
        or _owned_node_identity(after_path) != _owned_node_identity(opened)
        or after_path.st_nlink != 1
        or after_handle.st_size != len(raw_bytes)
    ):
        raise WorkflowError("restore journal changed during descriptor read")
    entries: list[dict[str, Any]] = []
    previous: Optional[str] = None
    for raw in raw_bytes.splitlines():
        try:
            entry = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowError("restore journal contains invalid JSON") from exc
        if (
            not isinstance(entry, dict)
            or set(entry) != JOURNAL_ENTRY_KEYS
            or entry.get("schema") != JOURNAL_SCHEMA
            or not isinstance(entry.get("event"), str)
            or not entry.get("event")
            or not isinstance(entry.get("data"), dict)
            or not isinstance(entry.get("recorded_at"), str)
        ):
            raise WorkflowError("restore journal schema is invalid")
        try:
            recorded_at = dt.datetime.fromisoformat(entry["recorded_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkflowError("restore journal timestamp is invalid") from exc
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise WorkflowError("restore journal timestamp is invalid")
        claimed = require_hash(entry.get("entry_hash"), "journal entry hash")
        unsigned = dict(entry)
        del unsigned["entry_hash"]
        if claimed != sha256_bytes(canonical_bytes(unsigned)):
            raise WorkflowError("restore journal hash chain is invalid")
        if entry.get("index") != len(entries) + 1 or entry.get("previous_entry_hash") != previous:
            raise WorkflowError("restore journal order/hash link is invalid")
        entries.append(entry)
        previous = claimed
    return entries, {
        "identity": list(_owned_node_identity(opened)),
        "size": len(raw_bytes),
        "sha256": sha256_bytes(raw_bytes),
    }


def verify_journal(journal_path: pathlib.Path) -> list[dict[str, Any]]:
    return _snapshot_journal(journal_path)[0]


def _finalize_lock_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_nlink),
    )


def _verify_finalize_lock_descriptor(
    handle: BinaryIO,
    expected_identity: tuple[int, int, int, int],
    expected_payload: bytes,
) -> None:
    metadata = os.fstat(handle.fileno())
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or _finalize_lock_identity(metadata) != expected_identity
    ):
        raise WorkflowError("finalize lock descriptor ownership changed")
    handle.seek(0)
    if handle.read(len(expected_payload) + 1) != expected_payload:
        raise WorkflowError("finalize lock ownership token changed")


def _verify_finalize_lock_path(
    lock_path: pathlib.Path,
    expected_identity: tuple[int, int, int, int],
    expected_payload: bytes,
) -> None:
    try:
        before = os.lstat(lock_path)
        if (
            stat.S_ISLNK(before.st_mode)
            or bool(getattr(before, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _finalize_lock_identity(before) != expected_identity
        ):
            raise WorkflowError("finalize lock path ownership changed; replacement retained")
        with lock_path.open("rb") as check:
            opened = os.fstat(check.fileno())
            payload = check.read(len(expected_payload) + 1)
        after = os.lstat(lock_path)
    except (FileNotFoundError, OSError) as exc:
        raise WorkflowError("finalize lock disappeared or became unreadable; replacement retained") from exc
    if (
        _finalize_lock_identity(opened) != expected_identity
        or _finalize_lock_identity(after) != expected_identity
        or payload != expected_payload
    ):
        raise WorkflowError("finalize lock path/token changed; replacement retained")


def _require_current_active_parent_finalize_lock(
    roots: Roots,
    proof: runtime_config_core.ActiveParentFinalizeLock,
) -> None:
    """Re-prove the current manager-owned lock by descriptor and named path."""

    if type(proof) is not runtime_config_core.ActiveParentFinalizeLock:
        raise WorkflowError("active parent finalize lock proof type is invalid")
    if type(proof.fd) is not int or proof.fd < 0:
        raise WorkflowError("active parent finalize lock descriptor is invalid")
    identity = proof.identity
    if (
        not isinstance(identity, tuple)
        or len(identity) != 4
        or any(type(value) is not int or value < 0 for value in identity)
        or identity[2] != stat.S_IFREG
        or identity[3] != 1
    ):
        raise WorkflowError("active parent finalize lock identity is invalid")
    if type(proof.payload) is not bytes or not proof.payload:
        raise WorkflowError("active parent finalize lock payload type is invalid")
    payload_hash = require_hash(
        proof.payload_sha256,
        "active parent finalize lock payload hash",
    )
    if (
        not isinstance(proof.owner_token, str)
        or re.fullmatch(r"[0-9a-f]{32}", proof.owner_token) is None
        or payload_hash != sha256_bytes(proof.payload)
        or proof.payload
        != canonical_bytes(
            {
                "schema": "oni.live-validation-finalize-lock.v1",
                "run_id": roots.run_id,
                "owner_token": proof.owner_token,
            }
        )
        + b"\n"
    ):
        raise WorkflowError("active parent finalize lock payload custody is invalid")

    try:
        descriptor_before = os.fstat(proof.fd)
        original_offset = os.lseek(proof.fd, 0, os.SEEK_CUR)
        try:
            os.lseek(proof.fd, 0, os.SEEK_SET)
            descriptor_payload = os.read(proof.fd, len(proof.payload) + 1)
        finally:
            os.lseek(proof.fd, original_offset, os.SEEK_SET)
        descriptor_after = os.fstat(proof.fd)
    except (OSError, ValueError) as exc:
        raise WorkflowError("active parent finalize lock descriptor is closed or unreadable") from exc
    if (
        not stat.S_ISREG(descriptor_before.st_mode)
        or descriptor_before.st_nlink != 1
        or _finalize_lock_identity(descriptor_before) != identity
        or _finalize_lock_identity(descriptor_after) != identity
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
        or descriptor_payload != proof.payload
    ):
        raise WorkflowError("active parent finalize lock descriptor custody changed")

    lock_path = roots.environment_dir / ".finalize.lock"
    assert_no_reparse(lock_path, roots.evidence_root)
    try:
        before = os.lstat(lock_path)
        with lock_path.open("rb") as named_handle:
            opened = os.fstat(named_handle.fileno())
            named_payload = named_handle.read(len(proof.payload) + 1)
        after = os.lstat(lock_path)
    except OSError as exc:
        raise WorkflowError("active parent finalize lock named path is unreadable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or _finalize_lock_identity(before) != identity
        or _finalize_lock_identity(opened) != identity
        or _finalize_lock_identity(after) != identity
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or named_payload != proof.payload
    ):
        raise WorkflowError("active parent finalize lock named-path custody changed")


@contextmanager
def exclusive_finalize_lock(
    roots: Roots,
    *,
    before_release_hook: Optional[Callable[[pathlib.Path], None]] = None,
    after_verify_before_unlink_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> Iterable[runtime_config_core.ActiveParentFinalizeLock]:
    environment_dir = roots.environment_dir
    if not environment_dir.is_dir():
        raise WorkflowError("prepared environment evidence is missing")
    assert_no_reparse(environment_dir, roots.evidence_root)
    lock_path = environment_dir / ".finalize.lock"
    try:
        handle = lock_path.open("x+b")
    except FileExistsError as exc:
        raise WorkflowError("another finalize operation or an unrecovered finalize lock exists") from exc
    token = uuid.uuid4().hex
    payload = canonical_bytes(
        {
            "schema": "oni.live-validation-finalize-lock.v1",
            "run_id": roots.run_id,
            "owner_token": token,
        }
    ) + b"\n"
    owner = os.fstat(handle.fileno())
    if not stat.S_ISREG(owner.st_mode) or owner.st_nlink != 1:
        handle.close()
        raise WorkflowError("finalize lock was not created as one owned regular file")
    owner_identity = _finalize_lock_identity(owner)
    proof = runtime_config_core.ActiveParentFinalizeLock(
        fd=handle.fileno(),
        identity=owner_identity,
        payload=payload,
        payload_sha256=sha256_bytes(payload),
        owner_token=token,
    )
    context_token = None
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        _require_current_active_parent_finalize_lock(roots, proof)
        context_token = _ACTIVE_PARENT_FINALIZE_LOCK.set(proof)
        yield proof
    finally:
        if context_token is not None:
            _ACTIVE_PARENT_FINALIZE_LOCK.reset(context_token)
        try:
            _verify_finalize_lock_descriptor(handle, owner_identity, payload)
        finally:
            handle.close()
        if before_release_hook is not None:
            if roots.test_root is None:
                raise WorkflowError("finalize lock release hooks are allowed only with TestRoot")
            before_release_hook(lock_path)
        _verify_finalize_lock_path(lock_path, owner_identity, payload)
        if after_verify_before_unlink_hook is not None:
            if roots.test_root is None:
                raise WorkflowError("finalize lock unlink hooks are allowed only with TestRoot")
            after_verify_before_unlink_hook(lock_path)
        release_claim = environment_dir / f".finalize.lock.release-{uuid.uuid4().hex[:16]}"
        if os.path.lexists(release_claim):
            raise WorkflowError("finalize lock release claim already exists")
        try:
            os.rename(lock_path, release_claim)
        except OSError as exc:
            raise WorkflowError("finalize lock could not be atomically reclaimed") from exc
        try:
            _verify_finalize_lock_path(release_claim, owner_identity, payload)
        except Exception as exc:
            if not os.path.lexists(lock_path):
                try:
                    os.rename(release_claim, lock_path)
                except OSError:
                    pass
            raise WorkflowError("finalize lock release ownership mismatch; claimed file retained or restored") from exc
        _unlink_owned_file(release_claim, owner_identity[:3])


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    roots = resolve_roots(args)
    counts = process_counts(args, roots)
    require_processes_stopped(counts)
    bridge = roots.oni_root / BRIDGE_RELATIVE
    gate = bridge_gate(bridge, require_bridge=False)
    pre_path = normalized(args.pre_boundary)
    clone_path = normalized(args.clone_manifest)
    source_save = normalized(args.source_save)
    loader_save = normalized(args.loader_save)
    if not path_within(pre_path, roots.boundary_report_root) or not path_within(clone_path, roots.evidence_root):
        raise WorkflowError("pre-boundary or clone manifest escaped the evidence roots")
    assert_no_reparse(pre_path, roots.boundary_report_root)
    assert_no_reparse(clone_path, roots.evidence_root)
    pre, clone = validate_clone_inputs(
        roots, pre_path, clone_path, source_save, loader_save, args.loader_stem
    )
    environment_dir = roots.environment_dir
    if environment_dir.exists() or os.path.lexists(environment_dir):
        raise WorkflowError("environment evidence directory already exists")
    assert_no_reparse(environment_dir.parent, roots.evidence_root, allow_missing_leaf=True)
    configs = manifest_config_records(roots.oni_root)
    probe = roots.oni_root / pathlib.Path(PROBE_RELATIVE)
    old_bridge = roots.oni_root / BRIDGE_RELATIVE
    archive_root = roots.oni_root / BRIDGE_ARCHIVE_RELATIVE
    assert_no_reparse(probe, roots.oni_root, allow_missing_leaf=True)
    assert_no_reparse(old_bridge, roots.oni_root, allow_missing_leaf=True)
    assert_no_reparse(archive_root, roots.oni_root, allow_missing_leaf=True)
    probe_snapshot = snapshot_tree(probe)
    bridge_snapshot = snapshot_tree(old_bridge)
    archive_root_snapshot = snapshot_tree(archive_root)
    manifest: dict[str, Any] = {
        "schema": ENVIRONMENT_SCHEMA,
        "run_id": roots.run_id,
        "created_at": now_iso(),
        "mode": "live" if roots.live_mode else "test_fixture",
        "path_disclosure": "root_ids_and_relative_paths_only",
        "roots": {
            "oni_root_id_lower": path_id(roots.oni_root, contract="lower"),
            "oni_root_id_upper": path_id(roots.oni_root, contract="upper"),
            "evidence_root_id_lower": path_id(roots.evidence_root, contract="lower"),
            "evidence_root_id_upper": path_id(roots.evidence_root, contract="upper"),
            "same_local_volume": True,
        },
        "bindings": {
            "pre_boundary": {
                "path_id": path_id(pre_path, contract="lower"),
                "sha256": sha256_file(pre_path),
                "oni_root_id": pre["oni_root_id"],
            },
            "clone_manifest": {
                "path_id": path_id(clone_path, contract="lower"),
                "sha256": sha256_file(clone_path),
                "target_stem_id": clone["target_stem_id"],
            },
            "source_save": {
                "path_id_lower": path_id(source_save, contract="lower"),
                "path_id_upper": path_id(source_save, contract="upper"),
                "state": file_state(source_save),
            },
            "source_preview": {
                "path_id_lower": path_id(source_save.with_suffix(".png"), contract="lower"),
                "path_id_upper": path_id(source_save.with_suffix(".png"), contract="upper"),
                "state": file_state(source_save.with_suffix(".png")),
            },
            "loader_save": {
                "path_id_lower": path_id(loader_save, contract="lower"),
                "path_id_upper": path_id(loader_save, contract="upper"),
                "stem_id": text_id(args.loader_stem),
                "state": file_state(loader_save),
            },
            "loader_preview": {
                "path_id_lower": path_id(loader_save.with_suffix(".png"), contract="lower"),
                "path_id_upper": path_id(loader_save.with_suffix(".png"), contract="upper"),
                "state": file_state(loader_save.with_suffix(".png")),
            },
        },
        "backup": {
            "configs": configs,
            "probe": {
                "relative": PROBE_RELATIVE,
                "path_id_lower": path_id(probe, contract="lower"),
                "path_id_upper": path_id(probe, contract="upper"),
                "backup_relative": "backup/probe" if probe_snapshot["exists"] else None,
                "snapshot": probe_snapshot,
            },
            "old_bridge": {
                "relative": BRIDGE_RELATIVE,
                "path_id_lower": path_id(old_bridge, contract="lower"),
                "path_id_upper": path_id(old_bridge, contract="upper"),
                "backup_relative": "backup/old-bridge" if bridge_snapshot["exists"] else None,
                "snapshot": bridge_snapshot,
            },
            "bridge_archive_root": {
                "relative": BRIDGE_ARCHIVE_RELATIVE,
                "path_id_lower": path_id(archive_root, contract="lower"),
                "path_id_upper": path_id(archive_root, contract="upper"),
                "snapshot": archive_root_snapshot,
            },
        },
        "preconditions": {
            "processes": counts,
            "bridge_gate": gate,
        },
    }
    if not args.apply:
        return {
            "schema": "oni.live-validation-environment-plan.v1",
            "status": "would_prepare",
            "run_id": roots.run_id,
            "write_count": 0,
            "config_binding_count": len(configs),
            "probe_file_count": int(probe_snapshot.get("file_count", 0)),
            "old_bridge_file_count": int(bridge_snapshot.get("file_count", 0)),
        }

    stage = environment_dir.parent / f".prep-{uuid.uuid4().hex[:16]}"
    if stage.exists() or os.path.lexists(stage):
        raise WorkflowError("prepare staging path already exists")
    stage.mkdir(parents=True)
    stage_owner: Optional[Mapping[str, Any]] = None
    stage_transferred = False
    try:
        for record in configs:
            if not record["exists"]:
                continue
            relative = validate_relative_path(record["relative"], "config backup")
            source = roots.oni_root / pathlib.Path(*relative.parts)
            target = stage / pathlib.Path(str(record["backup_relative"]))
            copied = copy_file_stable(source, target)
            if not states_equal(copied, record["state"]):
                raise WorkflowError("config backup verification failed")
        if probe_snapshot["exists"]:
            copy_tree_exact(probe, stage / "backup" / "probe", probe_snapshot)
        if bridge_snapshot["exists"]:
            copy_tree_exact(old_bridge, stage / "backup" / "old-bridge", bridge_snapshot)
        for record in configs:
            current = optional_file_state(roots.oni_root / pathlib.Path(record["relative"]))
            if current != {key: value for key, value in record.items() if key in {"exists", "state"}}:
                raise WorkflowError("config changed before prepare commit")
        verify_tree(probe, probe_snapshot, "probe source")
        verify_tree(old_bridge, bridge_snapshot, "old bridge source")
        manifest_path = stage / "environment-manifest.json"
        write_json_atomic(manifest_path, manifest, create_only=True)
        manifest_hash = sha256_file(manifest_path)
        append_journal(
            stage / "restore-journal.jsonl",
            "environment_prepared",
            {
                "environment_manifest_sha256": manifest_hash,
                "pre_boundary_sha256": manifest["bindings"]["pre_boundary"]["sha256"],
                "clone_manifest_sha256": manifest["bindings"]["clone_manifest"]["sha256"],
            },
        )
        stage_owner = capture_transaction_node(stage)
        if environment_dir.exists() or os.path.lexists(environment_dir):
            raise WorkflowError("environment evidence appeared concurrently")
        _rename_create_only(stage, environment_dir, "prepared environment")
        stage_transferred = True
    except Exception:
        if not stage_transferred and path_within(stage, roots.evidence_root):
            _remove_owned_tree(stage, stage_owner)
        raise
    return {
        "schema": "oni.live-validation-environment-prepare-result.v1",
        "status": "prepared",
        "run_id": roots.run_id,
        "environment_manifest_sha256": sha256_file(environment_dir / "environment-manifest.json"),
        "config_binding_count": len(configs),
        "probe_file_count": int(probe_snapshot.get("file_count", 0)),
        "old_bridge_file_count": int(bridge_snapshot.get("file_count", 0)),
    }


def load_environment_manifest(args: argparse.Namespace, roots: Roots) -> tuple[pathlib.Path, dict[str, Any], str]:
    manifest_path = normalized(args.manifest)
    expected = roots.environment_dir / "environment-manifest.json"
    if not same_path(manifest_path, expected):
        raise WorkflowError("environment manifest is not at its unique run path")
    assert_no_reparse(manifest_path, roots.evidence_root)
    manifest = load_json(manifest_path, "environment manifest")
    if (
        manifest.get("schema") != ENVIRONMENT_SCHEMA
        or manifest.get("run_id") != roots.run_id
        or manifest.get("mode") != ("live" if roots.live_mode else "test_fixture")
    ):
        raise WorkflowError("environment manifest does not bind this run/mode")
    root_binding = manifest.get("roots") if isinstance(manifest.get("roots"), dict) else {}
    if (
        root_binding.get("oni_root_id_lower") != path_id(roots.oni_root, contract="lower")
        or root_binding.get("oni_root_id_upper") != path_id(roots.oni_root, contract="upper")
        or root_binding.get("evidence_root_id_lower") != path_id(roots.evidence_root, contract="lower")
        or root_binding.get("evidence_root_id_upper") != path_id(roots.evidence_root, contract="upper")
    ):
        raise WorkflowError("environment manifest root identity is wrong")
    manifest_hash = sha256_file(manifest_path)
    journal = verify_journal(roots.environment_dir / "restore-journal.jsonl")
    if not journal or journal[0].get("data", {}).get("environment_manifest_sha256") != manifest_hash:
        raise WorkflowError("restore journal does not bind the environment manifest")
    return manifest_path, manifest, manifest_hash


def verify_base_bindings(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
) -> tuple[pathlib.Path, pathlib.Path]:
    bindings = manifest.get("bindings") if isinstance(manifest.get("bindings"), dict) else {}
    pre_path = normalized(args.pre_boundary)
    clone_path = normalized(args.clone_manifest)
    source_save = normalized(args.source_save)
    loader_save = normalized(args.loader_save)
    if (
        bindings.get("pre_boundary", {}).get("path_id") != path_id(pre_path, contract="lower")
        or bindings.get("pre_boundary", {}).get("sha256") != sha256_file(pre_path)
        or bindings.get("clone_manifest", {}).get("path_id") != path_id(clone_path, contract="lower")
        or bindings.get("clone_manifest", {}).get("sha256") != sha256_file(clone_path)
    ):
        raise WorkflowError("pre-boundary or clone manifest changed after prepare")
    # Runtime validation is expected to mutate the named loader clone.  Recheck
    # the immutable manifest, path identities, and original source content here,
    # but do not pretend the loader still has its pre-run bytes.
    validate_clone_inputs(
        roots,
        pre_path,
        clone_path,
        source_save,
        loader_save,
        args.loader_stem,
        require_initial_loader_content=False,
    )
    if (
        bindings.get("source_save", {}).get("path_id_lower") != path_id(source_save, contract="lower")
        or bindings.get("source_save", {}).get("state") != file_state(source_save)
        or bindings.get("source_preview", {}).get("state") != file_state(source_save.with_suffix(".png"))
        or bindings.get("loader_save", {}).get("path_id_lower") != path_id(loader_save, contract="lower")
        or bindings.get("loader_save", {}).get("stem_id") != text_id(args.loader_stem)
    ):
        raise WorkflowError("source/loader binding changed after prepare")
    return source_save, loader_save


def verify_backups(roots: Roots, manifest: Mapping[str, Any]) -> None:
    backup = manifest.get("backup") if isinstance(manifest.get("backup"), dict) else {}
    configs = backup.get("configs") if isinstance(backup.get("configs"), list) else []
    if len(configs) != len(CONFIG_RELATIVES):
        raise WorkflowError("config backup manifest is incomplete")
    for record in configs:
        relative = validate_relative_path(str(record.get("relative") or ""), "config backup")
        if record.get("relative") not in CONFIG_RELATIVES:
            raise WorkflowError("config backup manifest contains an unexpected path")
        if record.get("exists"):
            backup_relative = validate_relative_path(str(record.get("backup_relative") or ""), "config backup")
            path = roots.environment_dir / pathlib.Path(*backup_relative.parts)
            if file_state(path) != record.get("state"):
                raise WorkflowError("config backup was changed")
        elif record.get("backup_relative") is not None:
            raise WorkflowError("absent config unexpectedly names a backup")
        expected_path = roots.oni_root / pathlib.Path(*relative.parts)
        if record.get("path_id_lower") != path_id(expected_path, contract="lower"):
            raise WorkflowError("config backup path identity is wrong")
    for key, expected_relative in (("probe", PROBE_RELATIVE), ("old_bridge", BRIDGE_RELATIVE)):
        record = backup.get(key) if isinstance(backup.get(key), dict) else {}
        if record.get("relative") != expected_relative:
            raise WorkflowError(f"{key} backup path is invalid")
        snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
        if snapshot.get("exists"):
            relative = validate_relative_path(str(record.get("backup_relative") or ""), f"{key} backup")
            verify_tree(roots.environment_dir / pathlib.Path(*relative.parts), snapshot, f"{key} backup")
    archive = backup.get("bridge_archive_root") if isinstance(backup.get("bridge_archive_root"), dict) else {}
    archive_path = roots.oni_root / BRIDGE_ARCHIVE_RELATIVE
    if (
        archive.get("relative") != BRIDGE_ARCHIVE_RELATIVE
        or archive.get("path_id_lower") != path_id(archive_path, contract="lower")
        or archive.get("path_id_upper") != path_id(archive_path, contract="upper")
        or not isinstance(archive.get("snapshot"), dict)
    ):
        raise WorkflowError("bridge archive-root binding is invalid")


def validate_bootstrap(
    roots: Roots,
    manifest: Mapping[str, Any],
    bridge: pathlib.Path,
) -> tuple[pathlib.Path, dict[str, Any], Optional[pathlib.Path]]:
    bootstrap_path = bridge / "v27-runtime-bootstrap.json"
    bootstrap = load_json(bootstrap_path, "runtime bootstrap")
    if (
        bootstrap.get("schema") != BOOTSTRAP_SCHEMA
        or bootstrap.get("run_id") != roots.run_id
        or not isinstance(bootstrap.get("bridge_dir"), str)
        or not same_path(pathlib.Path(bootstrap["bridge_dir"]), bridge)
        or bootstrap.get("action_policy_disabled") is not True
    ):
        raise WorkflowError("runtime bootstrap does not bind this run/bridge")
    old_record = manifest["backup"]["old_bridge"]
    archive_record = manifest["backup"]["bridge_archive_root"]
    archive_snapshot = archive_record["snapshot"]
    archive_root = roots.oni_root / BRIDGE_ARCHIVE_RELATIVE
    archive_value = bootstrap.get("archived_previous_bridge")
    archive_path: Optional[pathlib.Path] = None
    if old_record["snapshot"]["exists"]:
        if not isinstance(archive_value, str) or not archive_value:
            raise WorkflowError("runtime bootstrap lost the archived previous bridge")
        archive_path = normalized(pathlib.Path(archive_value))
        if not path_within(archive_path, archive_root):
            raise WorkflowError("archived previous bridge escaped the archive root")
        assert_no_reparse(archive_path, roots.oni_root)
        verify_tree(archive_path, old_record["snapshot"], "archived previous bridge")
        verify_archive_remainder(archive_root, archive_snapshot, archive_path)
    elif archive_value not in (None, ""):
        raise WorkflowError("bootstrap names an old bridge that did not exist at prepare")
    else:
        verify_archive_remainder(archive_root, archive_snapshot, None)
    return bootstrap_path, bootstrap, archive_path


def validate_approval_bundle(
    bundle_path: pathlib.Path,
    bundle: Mapping[str, Any],
    command_id: str,
) -> dict[str, Any]:
    if set(bundle) != APPROVAL_BUNDLE_KEYS or bundle.get("schema") != APPROVAL_BUNDLE_SCHEMA:
        raise WorkflowError("automatic approval bundle shape/schema is invalid")
    command = bundle.get("final_command") if isinstance(bundle.get("final_command"), dict) else {}
    if (
        bundle.get("command_id") != command_id
        or bundle.get("action") != PRIORITY_ACTION
        or command.get("id") != command_id
        or command.get("action") != PRIORITY_ACTION
        or command.get("status") != "confirmed"
    ):
        raise WorkflowError("automatic approval bundle command binding is invalid")
    try:
        expected_intent_hash = command_intent_hash(command)
        expected_command_hash = contract_canonical_hash(command)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("automatic approval bundle command is not canonically hashable") from exc
    if (
        require_hash(bundle.get("command_intent_hash"), "bundle command intent hash") != expected_intent_hash
        or require_hash(bundle.get("final_command_hash"), "bundle final command hash") != expected_command_hash
    ):
        raise WorkflowError("automatic approval bundle command hashes are invalid")

    receipt = command.get("approval_receipt") if isinstance(command.get("approval_receipt"), dict) else {}
    receipt_hash = require_hash(receipt.get("receipt_hash"), "approval receipt hash")
    if (
        receipt_hash != contract_hash_without(receipt, "receipt_hash", "approval receipt")
        or bundle.get("approval_receipt_hash") != receipt_hash
    ):
        raise WorkflowError("automatic approval bundle receipt binding is invalid")

    source_policy = bundle.get("source_policy") if isinstance(bundle.get("source_policy"), dict) else {}
    if set(source_policy) != {"policy_hash", "file_sha256", "document"}:
        raise WorkflowError("automatic approval bundle policy freeze shape is invalid")
    policy = source_policy.get("document") if isinstance(source_policy.get("document"), dict) else {}
    policy_hash = require_hash(bundle.get("source_policy_hash"), "bundle source policy hash")
    command_source = command.get("source") if isinstance(command.get("source"), dict) else {}
    if (
        source_policy.get("policy_hash") != policy_hash
        or require_hash(source_policy.get("file_sha256"), "source policy file hash") is None
        or contract_canonical_hash(policy) != policy_hash
        or command_source.get("policy_hash") != policy_hash
        or receipt.get("policy_hash") != policy_hash
        or policy.get("schema") != "oni.action-policy.v1"
        or policy.get("actions_enabled") is not True
        or policy.get("suggestions_enabled") is not False
        or PRIORITY_ACTION not in (policy.get("allowed_actions") or [])
        or command.get("save_id_hash") not in (policy.get("allowed_save_hashes") or [])
    ):
        raise WorkflowError("automatic approval bundle source policy is invalid")

    source_frame = bundle.get("source_frame") if isinstance(bundle.get("source_frame"), dict) else {}
    if set(source_frame) != {"frame_id", "archive_file_sha256", "document"}:
        raise WorkflowError("automatic approval bundle source-frame freeze shape is invalid")
    frame = source_frame.get("document") if isinstance(source_frame.get("document"), dict) else {}
    frame_id = require_hash(source_frame.get("frame_id"), "bundle source frame id")
    require_hash(source_frame.get("archive_file_sha256"), "source frame archive file hash")
    if (
        command.get("source_frame_id") != frame_id
        or receipt.get("source_frame_id") != frame_id
        or set(frame) != {"schema", "frame_id", "source", "trust", "readiness", "blockers", "projections"}
        or frame.get("schema") != "oni.decision-frame.v2"
        or frame.get("frame_id") != frame_id
        or sha256_bytes(canonical_bytes({key: value for key, value in frame.items() if key != "frame_id"})) != frame_id
        or not isinstance(frame.get("trust"), dict)
        or frame["trust"].get("status") != "trusted"
    ):
        raise WorkflowError("automatic approval bundle embedded source frame is invalid")
    frame_source = frame.get("source") if isinstance(frame.get("source"), dict) else {}
    projections = frame.get("projections") if isinstance(frame.get("projections"), dict) else {}
    policy_projection = projections.get("policy") if isinstance(projections.get("policy"), dict) else {}
    expected_source = {
        "session_id": command.get("session_id"),
        "save_id_hash": command.get("save_id_hash"),
        "world_id": command.get("world_id"),
        "snapshot_id": command.get("source_snapshot_id"),
        "state_hash": command.get("state_hash"),
    }
    frame_snapshot_seq = frame_source.get("snapshot_seq")
    frame_seq = frame_source.get("seq")
    snapshot_seq_valid = (
        isinstance(frame_snapshot_seq, int)
        and not isinstance(frame_snapshot_seq, bool)
        and frame_snapshot_seq > 0
        and isinstance(frame_seq, int)
        and not isinstance(frame_seq, bool)
        and frame_snapshot_seq <= frame_seq
    )
    if (
        any(frame_source.get(key) != value for key, value in expected_source.items())
        or frame_source.get("seq") != command_source.get("state_seq")
        or not snapshot_seq_valid
        or frame_source.get("ts") != command_source.get("state_ts")
        or policy_projection.get("policy_hash") != policy_hash
    ):
        raise WorkflowError("automatic approval bundle source-frame identity is invalid")

    bundle_hash = require_hash(bundle.get("bundle_hash"), "approval bundle hash")
    if bundle_hash != contract_hash_without(bundle, "bundle_hash", "approval bundle"):
        raise WorkflowError("automatic approval bundle self-hash is invalid")
    return {
        "id": command_id,
        "bundle_path": bundle_path,
        "bundle_file_sha256": sha256_file(bundle_path),
        "bundle_hash": bundle_hash,
        "command_intent_hash": expected_intent_hash,
        "final_command_hash": expected_command_hash,
        "approval_receipt_hash": receipt_hash,
        "source_policy_hash": policy_hash,
        "source_frame_id": frame_id,
        "source_frame_archive_file_sha256": source_frame["archive_file_sha256"],
    }


def validate_lineage_and_bundles(
    args: argparse.Namespace,
    roots: Roots,
    bridge: pathlib.Path,
    pre_boundary_path: pathlib.Path,
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
) -> tuple[
    pathlib.Path,
    dict[str, Any],
    pathlib.Path,
    pathlib.Path,
    pathlib.Path,
    dict[str, Any],
    list[dict[str, Any]],
]:
    lineage_path = normalized(args.lineage_manifest)
    expected_lineage = bridge / "evidence" / "save-lineage" / roots.run_id / "manifest.json"
    if not same_path(lineage_path, expected_lineage):
        raise WorkflowError("lineage manifest is not at its unique run path")
    assert_no_reparse(lineage_path, bridge)
    lineage = load_json(lineage_path, "lineage manifest")
    if (
        lineage.get("schema") != LINEAGE_SCHEMA
        or lineage.get("status") != "active"
        or lineage.get("test_run_id") != roots.run_id
    ):
        raise WorkflowError("lineage manifest is not active for this run")
    derivative_save = normalized(pathlib.Path(str(lineage.get("save_path") or "")))
    save_root_for(derivative_save, roots.oni_root)
    derivative_preview = derivative_save.with_suffix(".png")
    if derivative_save.suffix.lower() != ".sav" or derivative_save in {source_save, loader_save}:
        raise WorkflowError("lineage derivative save identity is unsafe")
    if lineage.get("save_path_id") != path_id(derivative_save, contract="upper"):
        raise WorkflowError("lineage derivative path id is wrong")
    for path in (derivative_save, derivative_preview):
        if not path.is_file() or is_reparse(path):
            raise WorkflowError("lineage derivative pair is missing or unsafe")
    latest = lineage.get("latest") if isinstance(lineage.get("latest"), dict) else {}
    if latest.get("save_id_hash") != sha256_file(derivative_save):
        raise WorkflowError("lineage derivative disk hash is not the active tip")
    pre = load_json(pre_boundary_path, "pre-boundary report")
    pre_ids = {
        item.get("path_id")
        for item in (pre.get("saves") or {}).get("entries", [])
        if isinstance(item, dict)
    }
    if path_id(derivative_save, contract="lower") in pre_ids or path_id(derivative_preview, contract="lower") in pre_ids:
        raise WorkflowError("lineage derivative pair existed before the run")

    requested = list(args.command_id or [])
    if len(requested) != 2 or len(set(requested)) != 2 or any(COMMAND_ID_RE.fullmatch(item) is None for item in requested):
        raise WorkflowError("finalize requires exactly two unique valid --command-id values")
    steps = [args.forward_step_id, args.inverse_step_id]
    if (
        any(STEP_ID_RE.fullmatch(item or "") is None for item in steps)
        or steps[0] == steps[1]
    ):
        raise WorkflowError("finalize requires two distinct valid priority step ids")
    checkpoints = lineage.get("checkpoints") if isinstance(lineage.get("checkpoints"), list) else []
    bound_ids = {
        checkpoint.get("command", {}).get("id")
        for checkpoint in checkpoints
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("command"), dict)
    }
    if bound_ids != set(requested):
        raise WorkflowError("lineage checkpoint command ids do not exactly match finalize inputs")

    report_path = normalized(args.priority_pair_report)
    expected_report_path = lineage_path.parent / "priority-pair-finalization.json"
    if not same_path(report_path, expected_report_path):
        raise WorkflowError("priority pair report is not at its deterministic lineage path")
    assert_no_reparse(report_path, bridge)
    report = load_json(report_path, "priority pair report")
    if (
        set(report) != PAIR_REPORT_KEYS
        or report.get("schema") != PAIR_REPORT_SCHEMA
        or report.get("status") != "passed"
        or report.get("classification") is not None
        or report.get("verification_mode") != "offline_read_only"
        or report.get("fail_closed") is not True
    ):
        raise WorkflowError("priority pair report is not a passed offline fail-closed report")
    report_hash = require_hash(report.get("report_hash"), "priority pair report hash")
    if (
        report_hash != require_hash(args.priority_pair_report_hash, "expected priority pair report hash")
        or report_hash != contract_hash_without(report, "report_hash", "priority pair report")
    ):
        raise WorkflowError("priority pair report self-hash is invalid")
    report_manifest = report.get("manifest") if isinstance(report.get("manifest"), dict) else {}
    manifest_report_path = normalized(pathlib.Path(str(report_manifest.get("path") or "")))
    latest_checkpoint_hash = require_hash(lineage.get("latest_checkpoint_hash"), "lineage latest checkpoint hash")
    if (
        not same_path(manifest_report_path, lineage_path)
        or report_manifest.get("file_sha256") != sha256_file(lineage_path)
        or report_manifest.get("test_run_id") != roots.run_id
        or report_manifest.get("latest_checkpoint_hash") != latest_checkpoint_hash
    ):
        raise WorkflowError("priority pair report manifest binding is invalid")
    pair = report.get("pair") if isinstance(report.get("pair"), dict) else {}
    report_commands = pair.get("commands") if isinstance(pair.get("commands"), list) else []
    if (
        pair.get("forward_step") != steps[0]
        or pair.get("inverse_step") != steps[1]
        or len(report_commands) != 2
        or [item.get("step_id") if isinstance(item, dict) else None for item in report_commands] != steps
        or [item.get("id") if isinstance(item, dict) else None for item in report_commands] != requested
    ):
        raise WorkflowError("priority pair report does not bind the exact two requested steps/commands")
    latest = lineage.get("latest") if isinstance(lineage.get("latest"), dict) else {}
    final_tip = report.get("final_tip") if isinstance(report.get("final_tip"), dict) else {}
    if final_tip.get("save_id_hash") != latest.get("save_id_hash") or final_tip.get("save_id_hash") != sha256_file(derivative_save):
        raise WorkflowError("priority pair report final tip does not bind the lineage derivative")
    shutdown = report.get("shutdown") if isinstance(report.get("shutdown"), dict) else {}
    shutdown_queues = shutdown.get("queues") if isinstance(shutdown.get("queues"), dict) else {}
    if (
        shutdown.get("actions_enabled") is not False
        or shutdown.get("allowed_save_hash_count") != 0
        or shutdown.get("policy_file_sha256") != sha256_file(bridge / "action-policy.json")
        or shutdown_queues.get("temporary_file_count") != 0
        or any((shutdown_queues.get("nonterminal") or {}).get(name) != 0 for name in NONTERMINAL_QUEUES)
    ):
        raise WorkflowError("priority pair report shutdown gate is invalid or stale")

    command_evidence: list[dict[str, Any]] = []
    expected_bundle_hashes = list(args.command_bundle_hash or [])
    if len(expected_bundle_hashes) != 2:
        raise WorkflowError("finalize requires exactly two expected command bundle hashes")
    for value in expected_bundle_hashes:
        require_hash(value, "expected command bundle hash")
    for step_id, command_id, report_command, expected_bundle_hash in zip(
        steps,
        requested,
        report_commands,
        expected_bundle_hashes,
    ):
        if not isinstance(report_command, dict) or report_command.get("action") != PRIORITY_ACTION:
            raise WorkflowError("priority pair report command action is invalid")
        digest = hashlib.sha256(command_id.encode("utf-8", errors="strict")).hexdigest()
        bundle_path = bridge / "confirmed-command-evidence" / digest / "approval-bundle.json"
        assert_no_reparse(bundle_path, bridge)
        bundle = load_json(bundle_path, "automatic approval bundle")
        evidence = validate_approval_bundle(bundle_path, bundle, command_id)
        if evidence["bundle_hash"] != expected_bundle_hash:
            raise WorkflowError("automatic approval bundle does not match its expected checkpoint hash")
        final_command = bundle.get("final_command") if isinstance(bundle.get("final_command"), dict) else {}
        expected_command_path = bridge / "commands" / "done" / f"{command_id}.json"
        try:
            reported_command_path = resolve_bridge_relative_path(
                report_command.get("file"), bridge, f"{step_id} reported done command"
            )
        except ShutdownEvidenceError as exc:
            raise WorkflowError(f"priority pair report command path is invalid: {exc.code}") from exc
        if (
            not same_path(reported_command_path, expected_command_path)
            or report_command.get("file") != expected_command_path.relative_to(bridge).as_posix()
            or report_command.get("file_sha256") != sha256_file(expected_command_path)
            or report_command.get("args") != final_command.get("args")
            or contract_canonical_hash(load_json(expected_command_path, f"{step_id} done command"))
            != contract_canonical_hash(final_command)
        ):
            raise WorkflowError("priority pair report done-command binding is invalid")
        reported_bundle = (
            report_command.get("automatic_approval_bundle")
            if isinstance(report_command.get("automatic_approval_bundle"), dict)
            else {}
        )
        try:
            reported_bundle_path = resolve_bridge_relative_path(
                reported_bundle.get("path"), bridge, f"{step_id} reported approval bundle"
            )
        except ShutdownEvidenceError as exc:
            raise WorkflowError(f"priority pair report bundle path is invalid: {exc.code}") from exc
        if (
            not same_path(reported_bundle_path, bundle_path)
            or reported_bundle.get("path") != bundle_path.relative_to(bridge).as_posix()
            or reported_bundle.get("file_sha256") != evidence["bundle_file_sha256"]
            or reported_bundle.get("bundle_hash") != evidence["bundle_hash"]
            or report_command.get("command_intent_hash") != evidence["command_intent_hash"]
            or report_command.get("approval_receipt_hash") != evidence["approval_receipt_hash"]
            or report_command.get("policy_hash") != evidence["source_policy_hash"]
        ):
            raise WorkflowError("priority pair report automatic approval bundle binding is invalid")
        reported_frame = report_command.get("source_frame") if isinstance(report_command.get("source_frame"), dict) else {}
        frame_digest = evidence["source_frame_id"].removeprefix("sha256:")
        expected_frame_path = bridge / "decision-frames" / f"sha256-{frame_digest}.json"
        try:
            reported_frame_path = resolve_bridge_relative_path(
                reported_frame.get("archive_file"),
                bridge,
                f"{step_id} reported source frame",
                allow_missing_leaf=reported_frame.get("archive_currently_present") is False,
            )
        except ShutdownEvidenceError as exc:
            raise WorkflowError(f"priority pair report source-frame path is invalid: {exc.code}") from exc
        frame_document = bundle.get("source_frame", {}).get("document")
        frame_present = reported_frame.get("archive_currently_present")
        if frame_present is True:
            frame_disk_valid = (
                sha256_file(expected_frame_path) == evidence["source_frame_archive_file_sha256"]
                and isinstance(frame_document, dict)
                and contract_canonical_hash(load_json(expected_frame_path, f"{step_id} source frame"))
                == contract_canonical_hash(frame_document)
            )
        elif frame_present is False:
            frame_disk_valid = not os.path.lexists(expected_frame_path)
        else:
            frame_disk_valid = False
        if (
            reported_frame.get("frame_id") != evidence["source_frame_id"]
            or reported_frame.get("archive_file_sha256") != evidence["source_frame_archive_file_sha256"]
            or not same_path(reported_frame_path, expected_frame_path)
            or reported_frame.get("archive_file") != expected_frame_path.relative_to(bridge).as_posix()
            or not isinstance(frame_document, dict)
            or not frame_disk_valid
        ):
            raise WorkflowError("priority pair report source-frame bundle binding is invalid")

        freeze_path = lineage_path.parent / "priority-command-freezes" / f"{step_id}.json"
        assert_no_reparse(freeze_path, bridge)
        freeze = load_json(freeze_path, "priority command freeze")
        if (
            set(freeze) != COMMAND_FREEZE_KEYS
            or freeze.get("schema") != COMMAND_FREEZE_SCHEMA
            or freeze.get("step_id") != step_id
            or freeze.get("test_run_id") != roots.run_id
        ):
            raise WorkflowError("priority command freeze shape/binding is invalid")
        freeze_hash = require_hash(freeze.get("freeze_hash"), "priority command freeze hash")
        if freeze_hash != contract_hash_without(freeze, "freeze_hash", "priority command freeze"):
            raise WorkflowError("priority command freeze self-hash is invalid")
        frozen_bundle = freeze.get("approval_bundle") if isinstance(freeze.get("approval_bundle"), dict) else {}
        try:
            frozen_bundle_path = resolve_bridge_relative_path(
                frozen_bundle.get("path"), bridge, f"{step_id} frozen approval bundle"
            )
        except ShutdownEvidenceError as exc:
            raise WorkflowError(f"priority command freeze bundle path is invalid: {exc.code}") from exc
        frozen_certificate = (
            freeze.get("confirmation_certificate")
            if isinstance(freeze.get("confirmation_certificate"), dict)
            else {}
        )
        try:
            certificate_path = resolve_bridge_relative_path(
                frozen_certificate.get("path"),
                bridge,
                f"{step_id} frozen confirmation certificate",
                allow_missing_leaf=True,
            )
        except ShutdownEvidenceError as exc:
            raise WorkflowError(f"priority command freeze certificate path is invalid: {exc.code}") from exc
        certificate_relative = pathlib.PurePosixPath(str(frozen_certificate.get("path") or ""))
        certificate_document = (
            frozen_certificate.get("document")
            if isinstance(frozen_certificate.get("document"), dict)
            else {}
        )
        certificate_hash = require_hash(
            frozen_certificate.get("file_sha256"), "frozen confirmation certificate file hash"
        )
        certificate_present = os.path.lexists(certificate_path)
        certificate_disk_valid = (
            sha256_file(certificate_path) == certificate_hash
            and contract_canonical_hash(load_json(certificate_path, f"{step_id} confirmation certificate"))
            == contract_canonical_hash(certificate_document)
            if certificate_present
            else True
        )
        certificate_outcomes_hash = require_hash(
            frozen_certificate.get("outcomes_hash"), "frozen confirmation outcomes hash"
        )
        confirmation_records = freeze.get("confirmation_records")
        if (
            not same_path(frozen_bundle_path, bundle_path)
            or frozen_bundle.get("path") != bundle_path.relative_to(bridge).as_posix()
            or frozen_bundle.get("file_sha256") != evidence["bundle_file_sha256"]
            or frozen_bundle.get("bundle_hash") != evidence["bundle_hash"]
            or not isinstance(frozen_bundle.get("document"), dict)
            or contract_canonical_hash(frozen_bundle["document"]) != contract_canonical_hash(bundle)
            or len(certificate_relative.parts) != 3
            or certificate_relative.parts[0] != "sessions"
            or certificate_relative.parts[2] != "action-outcomes.json"
            or certificate_document.get("schema") != "oni.action-outcomes.v1"
            or certificate_document.get("outcomes_hash") != certificate_outcomes_hash
            or contract_hash_without(
                certificate_document, "outcomes_hash", f"{step_id} confirmation certificate"
            ) != certificate_outcomes_hash
            or not certificate_disk_valid
            or not isinstance(confirmation_records, list)
            or not confirmation_records
            or any(not isinstance(record, dict) for record in confirmation_records)
        ):
            raise WorkflowError("priority command freeze lost its immutable command evidence")
        assessment = _assess_frozen_confirmation(
            final_command,
            certificate_document,
            confirmation_records,
        )
        if (
            assessment.get("accepted") is not True
            or assessment.get("mode") != CONFIRMED_MUTATION_MODE
            or assessment.get("blockers") != []
        ):
            raise WorkflowError("priority command frozen confirmation does not reconstruct as accepted")
        confirmation = confirmation_summary(assessment)
        reported_freeze = report_command.get("lineage_freeze") if isinstance(report_command.get("lineage_freeze"), dict) else {}
        freeze_file_hash = sha256_file(freeze_path)
        if (
            not same_path(pathlib.Path(str(reported_freeze.get("path") or "")), freeze_path)
            or reported_freeze.get("file_sha256") != freeze_file_hash
            or reported_freeze.get("freeze_hash") != freeze_hash
        ):
            raise WorkflowError("priority pair report lineage-freeze binding is invalid")
        reported_confirmation = (
            report_command.get("confirmation")
            if isinstance(report_command.get("confirmation"), dict)
            else {}
        )
        reported_certificate = (
            reported_confirmation.get("certificate")
            if isinstance(reported_confirmation.get("certificate"), dict)
            else {}
        )
        try:
            reported_certificate_path = resolve_bridge_relative_path(
                reported_certificate.get("path"),
                bridge,
                f"{step_id} reported confirmation certificate",
                allow_missing_leaf=not certificate_present,
            )
        except ShutdownEvidenceError as exc:
            raise WorkflowError(f"priority pair report certificate path is invalid: {exc.code}") from exc
        if (
            not same_path(reported_certificate_path, certificate_path)
            or reported_certificate.get("path") != certificate_relative.as_posix()
            or reported_certificate.get("file_sha256") != certificate_hash
            or reported_certificate.get("outcomes_hash") != certificate_outcomes_hash
            or reported_confirmation.get("attestation_hash") != confirmation.get("attestation_hash")
            or reported_confirmation.get("ack_sequences") != confirmation.get("ack_sequences")
            or reported_confirmation.get("witness") != confirmation.get("witness")
        ):
            raise WorkflowError("priority pair report confirmation-certificate binding is invalid")
        evidence.update(
            {
                "step_id": step_id,
                "command_path": expected_command_path,
                "command_file_sha256": report_command["file_sha256"],
                "source_frame_path": expected_frame_path,
                "source_frame_currently_present": frame_present,
                "confirmation_certificate_path": certificate_path,
                "confirmation_certificate_currently_present": certificate_present,
                "confirmation_certificate_file_sha256": certificate_hash,
                "confirmation_certificate_outcomes_hash": certificate_outcomes_hash,
                "freeze_path": freeze_path,
                "freeze_file_sha256": freeze_file_hash,
                "freeze_hash": freeze_hash,
            }
        )
        command_evidence.append(evidence)
    return (
        lineage_path,
        lineage,
        derivative_save,
        derivative_preview,
        report_path,
        report,
        command_evidence,
    )


def _transaction_node_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode))


def _transaction_tree_identities(root: pathlib.Path) -> list[tuple[str, str, int, int, int]]:
    identities: list[tuple[str, str, int, int, int]] = []
    # Traverse through the extended-length OS path on Windows.  The logical
    # paths are rebuilt under ``root`` so callers retain stable relative names.
    io_root = pathlib.Path(_io_path(root))
    logical_paths = [
        root.joinpath(*entry.relative_to(io_root).parts)
        for entry in io_root.rglob("*")
    ]
    for path in sorted((root, *logical_paths), key=lambda item: str(item).casefold()):
        metadata = _lstat(path)
        if is_reparse(path):
            raise WorkflowError("transaction tree contains a reparse point")
        kind = "directory" if stat.S_ISDIR(metadata.st_mode) else "file" if stat.S_ISREG(metadata.st_mode) else "other"
        if kind == "other":
            raise WorkflowError("transaction tree contains an unsupported node")
        relative = "." if path == root else path.relative_to(root).as_posix()
        identities.append(
            (relative, kind, int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode))
        )
    return identities


def capture_transaction_node(path: pathlib.Path) -> dict[str, Any]:
    path = normalized(path)
    before = _lstat(path)
    if is_reparse(path):
        raise WorkflowError("transaction node is a reparse point")
    identity = _transaction_node_identity(before)
    if stat.S_ISREG(before.st_mode):
        if before.st_nlink != 1:
            raise WorkflowError("transaction file is hardlinked")
        state = {
            "kind": "file",
            "identity": list(identity),
            "file": file_state(path),
        }
    elif stat.S_ISDIR(before.st_mode):
        identities_before = _transaction_tree_identities(path)
        tree = snapshot_tree(path)
        identities_after = _transaction_tree_identities(path)
        if identities_before != identities_after:
            raise WorkflowError("transaction tree changed while ownership was captured")
        state = {
            "kind": "directory",
            "identity": list(identity),
            "tree": tree,
            "node_identities": [list(item) for item in identities_after],
        }
    else:
        raise WorkflowError("transaction node must be a regular file or directory")
    after = _lstat(path)
    if _transaction_node_identity(after) != identity:
        raise WorkflowError("transaction node identity changed while it was captured")
    return state


def transaction_node_matches(path: pathlib.Path, expected: Mapping[str, Any]) -> bool:
    # A directory rename can be durably complete before every descendant can
    # immediately be reopened by name on Windows. Retry only an exceptional
    # descriptor capture once; an observed content/identity mismatch is final.
    # This preserves fail-closed comparison while avoiding a false
    # outcome_unknown caused solely by that post-rename visibility interval.
    for attempt in range(2):
        try:
            return capture_transaction_node(path) == dict(expected)
        except (OSError, WorkflowError):
            if attempt:
                return False
    return False


def claim_transaction_node(
    path: pathlib.Path,
    expected: Mapping[str, Any],
    claim: pathlib.Path,
    *,
    before_claim_hook: Optional[Callable[[], None]] = None,
) -> pathlib.Path:
    """Atomically reclaim a pathname, then validate identity before mutation."""

    path = normalized(path)
    claim = normalized(claim)
    claim.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(claim):
        raise WorkflowError("rollback ownership claim already exists")
    if before_claim_hook is not None:
        before_claim_hook()
    try:
        os.rename(path, claim)
    except OSError as exc:
        raise WorkflowError("rollback could not atomically claim its output") from exc
    if transaction_node_matches(claim, expected):
        return claim
    try:
        if os.path.lexists(path):
            raise WorkflowError("rollback external replacement retained in recovery claim")
        os.rename(claim, path)
    except OSError as exc:
        raise WorkflowError("rollback external replacement retained in recovery claim") from exc
    raise WorkflowError("rollback ownership mismatch; external pathname restored unchanged")


FINALIZE_TRANSACTION_PLAN_KEYS = frozenset(
    {
        "schema",
        "transaction_id",
        "run_id",
        "environment_manifest_sha256",
        "runtime_binding_sha256",
        "steps",
        "plan_hash",
    }
)
FINALIZE_TRANSACTION_STEP_KEYS = frozenset(
    {
        "index",
        "kind",
        "label",
        "source_path_id_lower",
        "target_path_id_lower",
        "source_owner_hash",
        "target_initial_owner_hash",
        "desired_mtime_ns",
    }
)
FINALIZE_TRANSACTION_EVENTS = frozenset(
    {
        "restore_transaction_started",
        "restore_step_intent",
        "restore_step_completed",
        "restore_transaction_commit_prepared",
        "restore_transaction_physical_committed",
        "restore_rollback_started",
        "restore_rollback_step",
        "restore_rollback_complete",
        "restore_rollback_failed",
    }
)


def _optional_transaction_owner_hash(path: pathlib.Path) -> Optional[str]:
    if not os.path.lexists(path):
        return None
    return contract_canonical_hash(capture_transaction_node(path))


def _finalize_transaction_plan_document(
    roots: Roots,
    manifest_hash: str,
    runtime_binding_sha256: str,
    operations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for index, operation in enumerate(operations, start=1):
        kind = operation.get("kind")
        label = operation.get("label")
        source = operation.get("source")
        target = operation.get("target")
        desired_mtime = operation.get("value") if kind == "set_mtime" else None
        if (
            kind not in {"move", "copy_file", "copy_tree", "set_mtime"}
            or not isinstance(label, str)
            or STEP_ID_RE.fullmatch(label) is None
            or not isinstance(source, pathlib.Path)
            or not isinstance(target, pathlib.Path)
            or (
                desired_mtime is not None
                and (
                    isinstance(desired_mtime, bool)
                    or not isinstance(desired_mtime, int)
                    or desired_mtime < 0
                )
            )
        ):
            raise WorkflowError("finalize transaction operation plan is invalid")
        source = normalized(source)
        target = normalized(target)
        source_owner_hash = _optional_transaction_owner_hash(source)
        if source_owner_hash is None:
            raise WorkflowError(f"finalize transaction plan source is missing: {label}")
        steps.append(
            {
                "index": index,
                "kind": kind,
                "label": label,
                "source_path_id_lower": path_id(source, contract="lower"),
                "target_path_id_lower": path_id(target, contract="lower"),
                "source_owner_hash": source_owner_hash,
                "target_initial_owner_hash": _optional_transaction_owner_hash(target),
                "desired_mtime_ns": desired_mtime,
            }
        )
    transaction_seed = canonical_bytes(
        {
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "runtime_binding_sha256": runtime_binding_sha256,
            "steps": steps,
        }
    )
    document = {
        "schema": FINALIZE_TRANSACTION_PLAN_SCHEMA,
        "transaction_id": "finalize-" + hashlib.sha256(transaction_seed).hexdigest()[:32],
        "run_id": roots.run_id,
        "environment_manifest_sha256": manifest_hash,
        "runtime_binding_sha256": runtime_binding_sha256,
        "steps": steps,
    }
    document["plan_hash"] = contract_canonical_hash(document)
    return document


def _validate_finalize_transaction_plan(
    plan: Mapping[str, Any],
    *,
    roots: Roots,
    manifest_hash: str,
    runtime_binding_sha256: str,
) -> dict[str, Any]:
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    transaction_seed = canonical_bytes(
        {
            "run_id": plan.get("run_id"),
            "environment_manifest_sha256": plan.get("environment_manifest_sha256"),
            "runtime_binding_sha256": plan.get("runtime_binding_sha256"),
            "steps": steps,
        }
    )
    expected_transaction_id = (
        "finalize-" + hashlib.sha256(transaction_seed).hexdigest()[:32]
    )
    if (
        set(plan) != FINALIZE_TRANSACTION_PLAN_KEYS
        or plan.get("schema") != FINALIZE_TRANSACTION_PLAN_SCHEMA
        or plan.get("transaction_id") != expected_transaction_id
        or plan.get("run_id") != roots.run_id
        or plan.get("environment_manifest_sha256") != manifest_hash
        or plan.get("runtime_binding_sha256") != runtime_binding_sha256
        or not steps
        or plan.get("plan_hash")
        != contract_hash_without(plan, "plan_hash", "finalize transaction plan")
    ):
        raise WorkflowError("finalize transaction plan shape or binding is invalid")
    labels: set[str] = set()
    for index, step in enumerate(steps, start=1):
        desired_mtime = step.get("desired_mtime_ns") if isinstance(step, Mapping) else None
        if (
            not isinstance(step, Mapping)
            or set(step) != FINALIZE_TRANSACTION_STEP_KEYS
            or step.get("index") != index
            or step.get("kind") not in {"move", "copy_file", "copy_tree", "set_mtime"}
            or not isinstance(step.get("label"), str)
            or STEP_ID_RE.fullmatch(str(step.get("label"))) is None
            or step.get("label") in labels
            or HASH_RE.fullmatch(str(step.get("source_path_id_lower"))) is None
            or HASH_RE.fullmatch(str(step.get("target_path_id_lower"))) is None
            or HASH_RE.fullmatch(str(step.get("source_owner_hash"))) is None
            or (
                step.get("target_initial_owner_hash") is not None
                and HASH_RE.fullmatch(str(step.get("target_initial_owner_hash"))) is None
            )
            or (
                step.get("kind") == "set_mtime"
                and (
                    isinstance(desired_mtime, bool)
                    or not isinstance(desired_mtime, int)
                    or desired_mtime < 0
                )
            )
            or (step.get("kind") != "set_mtime" and desired_mtime is not None)
        ):
            raise WorkflowError("finalize transaction plan step is invalid")
        labels.add(str(step["label"]))
    return copy.deepcopy(dict(plan))


def _publish_or_verify_finalize_transaction_plan(
    path: pathlib.Path,
    expected: Mapping[str, Any],
    *,
    roots: Roots,
    manifest_hash: str,
    runtime_binding_sha256: str,
) -> tuple[dict[str, Any], str]:
    if os.path.lexists(path):
        existing = load_json(path, "finalize transaction plan")
        validated = _validate_finalize_transaction_plan(
            existing,
            roots=roots,
            manifest_hash=manifest_hash,
            runtime_binding_sha256=runtime_binding_sha256,
        )
        if validated != dict(expected):
            raise WorkflowError("existing finalize transaction plan conflicts with exact prestate")
    else:
        write_json_atomic(path, dict(expected), create_only=True)
        validated = _validate_finalize_transaction_plan(
            load_json(path, "finalize transaction plan"),
            roots=roots,
            manifest_hash=manifest_hash,
            runtime_binding_sha256=runtime_binding_sha256,
        )
        if validated != dict(expected):
            raise WorkflowError("published finalize transaction plan changed")
    return validated, sha256_file(path)


def _append_finalize_transaction_journal(
    plan: Mapping[str, Any],
    plan_file_sha256: str,
    attempt_id: str,
) -> Callable[[pathlib.Path, str, Mapping[str, Any]], Mapping[str, Any]]:
    def append(path: pathlib.Path, event: str, data: Mapping[str, Any]) -> Mapping[str, Any]:
        enriched = dict(data)
        enriched.update(
            {
                "finalize_transaction_id": plan["transaction_id"],
                "finalize_attempt_id": attempt_id,
                "finalize_plan_hash": plan["plan_hash"],
                "finalize_plan_file_sha256": plan_file_sha256,
            }
        )
        return append_journal(path, event, enriched)

    return append


def _classify_finalize_transaction_journal(
    roots: Roots,
    plan: Mapping[str, Any],
    plan_file_sha256: str,
) -> str:
    binding_keys = {
        "finalize_transaction_id",
        "finalize_attempt_id",
        "finalize_plan_hash",
        "finalize_plan_file_sha256",
    }
    business_keys = {
        "restore_transaction_started": {"transaction_kind", "expected_step_count"},
        "restore_step_intent": {"kind", "label"},
        "restore_step_completed": {"step", "label"},
        "restore_transaction_commit_prepared": {"step_count", "state"},
        "restore_transaction_physical_committed": {"step_count", "state"},
        "restore_rollback_started": {"undo_count"},
        "restore_rollback_complete": {
            "failed_count",
            "journal_failure_count",
            "recovery_required",
        },
        "restore_rollback_failed": {
            "failed_count",
            "journal_failure_count",
            "recovery_required",
        },
    }
    entries = verify_journal(roots.environment_dir / "restore-journal.jsonl")
    selected: list[dict[str, Any]] = []
    open_finalize_attempt = False
    for entry in entries:
        event = entry.get("event")
        if event not in FINALIZE_TRANSACTION_EVENTS:
            if open_finalize_attempt:
                raise WorkflowError("restore journal interleaves a foreign event inside a finalize attempt")
            continue
        data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}
        has_finalize_binding = any(str(key).startswith("finalize_") for key in data)
        if not has_finalize_binding:
            if "abort_transaction_id" in data:
                if open_finalize_attempt:
                    raise WorkflowError("restore journal interleaves an abort event inside a finalize attempt")
                continue
            raise WorkflowError("restore journal contains an unbound transaction event")
        if (
            data.get("finalize_transaction_id") != plan.get("transaction_id")
            or data.get("finalize_plan_hash") != plan.get("plan_hash")
            or data.get("finalize_plan_file_sha256") != plan_file_sha256
            or not isinstance(data.get("finalize_attempt_id"), str)
            or re.fullmatch(r"attempt-[0-9a-f]{32}", str(data.get("finalize_attempt_id")))
            is None
        ):
            raise WorkflowError("restore journal transaction binding is invalid")
        expected_data_keys = business_keys.get(str(event))
        if event == "restore_rollback_step":
            expected_data_keys = (
                {"label", "status"}
                if data.get("status") == "restored"
                else {"label", "status", "error_type", "recovery_required"}
            )
        if expected_data_keys is None or set(data) != binding_keys | expected_data_keys:
            raise WorkflowError("restore journal transaction event data shape is invalid")
        if event == "restore_transaction_started":
            if open_finalize_attempt:
                raise WorkflowError("restore journal starts a nested finalize attempt")
            open_finalize_attempt = True
        selected.append(entry)
        if event in {
            "restore_transaction_physical_committed",
            "restore_rollback_complete",
            "restore_rollback_failed",
        }:
            open_finalize_attempt = False
    if not selected:
        return "no_attempt"

    attempts: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    current_id: Optional[str] = None
    for entry in selected:
        data = entry["data"]
        attempt_id = str(data["finalize_attempt_id"])
        if entry.get("event") == "restore_transaction_started":
            if attempt_id in seen:
                raise WorkflowError("restore journal duplicates a finalize attempt id")
            seen.add(attempt_id)
            attempts.append([])
            current_id = attempt_id
        if current_id is None or attempt_id != current_id:
            raise WorkflowError("restore journal finalize attempts are interleaved or truncated")
        attempts[-1].append(entry)

    expected_steps = plan["steps"]
    statuses: list[str] = []
    for attempt in attempts:
        if attempt[0].get("event") != "restore_transaction_started":
            raise WorkflowError("restore journal finalize attempt has no start record")
        started_data = attempt[0]["data"]
        if (
            started_data.get("transaction_kind") != "finalize_restore"
            or started_data.get("expected_step_count") != len(expected_steps)
        ):
            raise WorkflowError("restore journal finalize start does not bind the plan")
        prepared = [entry for entry in attempt if entry.get("event") == "restore_transaction_commit_prepared"]
        committed = [entry for entry in attempt if entry.get("event") == "restore_transaction_physical_committed"]
        rollback_started = [entry for entry in attempt if entry.get("event") == "restore_rollback_started"]
        rollback_steps = [entry for entry in attempt if entry.get("event") == "restore_rollback_step"]
        rollback_complete = [entry for entry in attempt if entry.get("event") == "restore_rollback_complete"]
        rollback_failed = [entry for entry in attempt if entry.get("event") == "restore_rollback_failed"]
        if committed:
            expected_events = ["restore_transaction_started"]
            for _step in expected_steps:
                expected_events.extend(("restore_step_intent", "restore_step_completed"))
            expected_events.extend(
                (
                    "restore_transaction_commit_prepared",
                    "restore_transaction_physical_committed",
                )
            )
            if (
                [entry.get("event") for entry in attempt] != expected_events
                or len(prepared) != 1
                or len(committed) != 1
                or rollback_started
                or rollback_steps
                or rollback_complete
                or rollback_failed
                or prepared[0]["data"].get("step_count") != len(expected_steps)
                or committed[0]["data"].get("step_count") != len(expected_steps)
                or prepared[0]["data"].get("state")
                != "physical_committed_pending_journal"
                or committed[0]["data"].get("state") != "journal_confirmed"
            ):
                raise WorkflowError("restore journal physical commit is ambiguous")
            for index, expected in enumerate(expected_steps, start=1):
                intent_data = attempt[(index * 2) - 1]["data"]
                completed_data = attempt[index * 2]["data"]
                if (
                    intent_data.get("kind") != expected["kind"]
                    or intent_data.get("label") != expected["label"]
                    or completed_data.get("step") != index
                    or completed_data.get("label") != expected["label"]
                ):
                    raise WorkflowError("restore journal committed step grammar is invalid")
            statuses.append("physical_committed")
            continue
        if rollback_complete:
            terminal = rollback_complete[0]["data"] if len(rollback_complete) == 1 else {}
            if (
                len(rollback_complete) != 1
                or rollback_complete[0] is not attempt[-1]
                or len(rollback_started) != 1
                or rollback_failed
                or terminal.get("failed_count") != 0
                or terminal.get("journal_failure_count") != 0
                or terminal.get("recovery_required") is not False
            ):
                raise WorkflowError("restore journal clean rollback epoch is invalid")
            rollback_start_index = attempt.index(rollback_started[0])
            forward = attempt[1:rollback_start_index]
            completed_labels: list[str] = []
            dangling_intent_label: Optional[str] = None
            cursor = 0
            while cursor < len(forward):
                event = forward[cursor].get("event")
                if event == "restore_transaction_commit_prepared":
                    if cursor != len(forward) - 1 or len(completed_labels) != len(expected_steps):
                        raise WorkflowError("restore journal rollback has a misplaced commit preparation")
                    break
                step_index = len(completed_labels)
                if event != "restore_step_intent" or step_index >= len(expected_steps):
                    raise WorkflowError("restore journal rollback forward prefix is invalid")
                expected = expected_steps[step_index]
                intent_data = forward[cursor]["data"]
                if (
                    intent_data.get("kind") != expected["kind"]
                    or intent_data.get("label") != expected["label"]
                ):
                    raise WorkflowError("restore journal rollback intent prefix conflicts with the plan")
                if cursor + 1 >= len(forward):
                    dangling_intent_label = expected["label"]
                    cursor += 1
                    break
                next_entry = forward[cursor + 1]
                if next_entry.get("event") != "restore_step_completed":
                    dangling_intent_label = expected["label"]
                    cursor += 1
                    if cursor != len(forward):
                        raise WorkflowError("restore journal has events after a dangling intent")
                    break
                completed_data = next_entry["data"]
                if (
                    completed_data.get("step") != step_index + 1
                    or completed_data.get("label") != expected["label"]
                ):
                    raise WorkflowError("restore journal rollback completion prefix is out of order")
                completed_labels.append(expected["label"])
                cursor += 2
            undo_count = rollback_started[0]["data"].get("undo_count")
            if isinstance(undo_count, bool) or not isinstance(undo_count, int):
                raise WorkflowError("restore journal rollback undo count is invalid")
            physical_labels = list(completed_labels)
            if undo_count == len(completed_labels) + 1 and dangling_intent_label is not None:
                physical_labels.append(dangling_intent_label)
            elif undo_count != len(completed_labels):
                raise WorkflowError("restore journal rollback undo count conflicts with the forward prefix")
            restored_labels = [entry["data"].get("label") for entry in rollback_steps]
            if (
                len(rollback_steps) != undo_count
                or restored_labels != list(reversed(physical_labels))
                or any(entry["data"].get("status") != "restored" for entry in rollback_steps)
            ):
                raise WorkflowError("restore journal rollback steps are not the exact reverse physical prefix")
            statuses.append("clean_rollback")
            continue
        raise WorkflowError("restore journal finalize attempt has no trustworthy terminal state")

    if any(status != "clean_rollback" for status in statuses[:-1]):
        raise WorkflowError("restore journal started another attempt after a committed or ambiguous epoch")
    return statuses[-1]


class FileTransaction:
    def __init__(
        self,
        journal: pathlib.Path,
        recovery_root: pathlib.Path,
        fault_after: int = 0,
        *,
        after_step_hook: Optional[Callable[[int, str], None]] = None,
        after_physical_hook: Optional[Callable[[str, pathlib.Path], None]] = None,
        journal_append: Optional[
            Callable[[pathlib.Path, str, Mapping[str, Any]], Mapping[str, Any]]
        ] = None,
        before_undo_claim_hook: Optional[Callable[[str, pathlib.Path], None]] = None,
        before_publish_hook: Optional[Callable[[str, pathlib.Path], None]] = None,
        recovery_context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.journal = journal
        self.recovery_root = recovery_root
        self.fault_after = fault_after
        self.completed = 0
        self.undos: list[tuple[str, Callable[[], None]]] = []
        self.committed = False
        self.after_step_hook = after_step_hook
        self.after_physical_hook = after_physical_hook
        self.journal_append = journal_append or append_journal
        self.state = "prepared"
        self.before_undo_claim_hook = before_undo_claim_hook
        self.before_publish_hook = before_publish_hook
        self.recovery_context = dict(recovery_context or {})

    def _claim_hook(self, label: str, path: pathlib.Path) -> Optional[Callable[[], None]]:
        if self.before_undo_claim_hook is None:
            return None
        return lambda: self.before_undo_claim_hook(label, path)

    def _append(self, event: str, data: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.journal_append(self.journal, event, data)

    def _publish_recovery_state(self, state: str, failures: Sequence[str]) -> None:
        payload = {
            "schema": self.recovery_context.get(
                "schema",
                "oni.live-validation-transaction-recovery.v1",
            ),
            "state": state,
            "physical_step_count": self.completed,
            "failures": list(failures),
            "recovery_required": True,
            **self.recovery_context,
        }
        recovery_path = self.recovery_root / "transaction-recovery.json"
        try:
            write_json_once(recovery_path, payload, "transaction recovery")
        except Exception:
            # Physical rollback must continue even when all evidence channels
            # are impaired.  The caller still receives outcome_unknown.
            pass

    def _completed(self, label: str) -> None:
        self.completed += 1
        self._append("restore_step_completed", {"step": self.completed, "label": label})
        if self.after_step_hook is not None:
            self.after_step_hook(self.completed, label)
        if self.fault_after and self.completed == self.fault_after:
            raise WorkflowError("injected TestRoot transaction failure")

    def move(self, source: pathlib.Path, target: pathlib.Path, label: str) -> None:
        source = normalized(source)
        target = normalized(target)
        if not source.exists() or target.exists() or os.path.lexists(target):
            raise WorkflowError(f"transaction move precondition failed: {label}")
        committed = capture_transaction_node(source)
        rollback_index = len(self.undos)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._append("restore_step_intent", {"kind": "move", "label": label})
        if self.before_publish_hook is not None:
            self.before_publish_hook(label, target)
        _rename_create_only(source, target, f"transaction move {label}")

        def undo() -> None:
            # Keep claims deliberately short: the runtime bridge contains
            # mandatory 64-hex path segments and recovery must remain below
            # the legacy Win32 MAX_PATH boundary used by supported hosts.
            claim = self.recovery_root / "r" / f"m-{rollback_index:03d}"
            claimed = claim_transaction_node(
                target,
                committed,
                claim,
                before_claim_hook=self._claim_hook(label, target),
            )
            if os.path.lexists(source):
                raise WorkflowError(f"rollback move source was externally recreated; owned output retained: {label}")
            source.parent.mkdir(parents=True, exist_ok=True)
            os.rename(claimed, source)
            if not transaction_node_matches(source, committed):
                raise WorkflowError(f"rollback move restoration was not durably visible: {label}")

        self.undos.append((label, undo))
        if self.after_physical_hook is not None:
            self.after_physical_hook(label, target)
        if not transaction_node_matches(target, committed):
            raise WorkflowError(f"transaction move ownership changed after publication: {label}")
        self._completed(label)

    def copy_file(self, source: pathlib.Path, target: pathlib.Path, label: str) -> None:
        if target.exists() or os.path.lexists(target):
            raise WorkflowError(f"transaction copy target exists: {label}")
        self._append("restore_step_intent", {"kind": "copy_file", "label": label})
        rollback_index = len(self.undos)
        committed_holder: list[Mapping[str, Any]] = []

        def undo() -> None:
            if not committed_holder:
                raise WorkflowError(f"rollback copy publication ownership is unknown: {label}")
            committed = committed_holder[0]
            rollback_target = self.recovery_root / "r" / f"f-{rollback_index:03d}"
            claimed = claim_transaction_node(
                target,
                committed,
                rollback_target,
                before_claim_hook=self._claim_hook(label, target),
            )
            if os.path.lexists(target) or not transaction_node_matches(claimed, committed):
                raise WorkflowError(f"rollback copy reclaim was not durably visible: {label}")

        def published(owner: Mapping[str, Any]) -> None:
            committed_holder.append(dict(owner))
            self.undos.append((label, undo))

        copy_file_stable(
            source,
            target,
            before_commit_hook=(
                (lambda: self.before_publish_hook(label, target))
                if self.before_publish_hook is not None
                else None
            ),
            published_owner_hook=published,
            after_publish_hook=(
                (lambda published_target, _temporary: self.after_physical_hook(label, published_target))
                if self.after_physical_hook is not None
                else None
            ),
        )
        self._completed(label)

    def copy_tree(self, source: pathlib.Path, target: pathlib.Path, expected: Mapping[str, Any], label: str) -> None:
        if target.exists() or os.path.lexists(target):
            raise WorkflowError(f"transaction tree target exists: {label}")
        self._append("restore_step_intent", {"kind": "copy_tree", "label": label})
        rollback_index = len(self.undos)
        committed_holder: list[Mapping[str, Any]] = []

        def undo() -> None:
            if not committed_holder:
                raise WorkflowError(f"rollback tree publication ownership is unknown: {label}")
            committed = committed_holder[0]
            rollback_target = self.recovery_root / "r" / f"t-{rollback_index:03d}"
            claimed = claim_transaction_node(
                target,
                committed,
                rollback_target,
                before_claim_hook=self._claim_hook(label, target),
            )
            if os.path.lexists(target) or not transaction_node_matches(claimed, committed):
                raise WorkflowError(f"rollback tree reclaim was not durably visible: {label}")

        def published(owner: Mapping[str, Any]) -> None:
            committed_holder.append(dict(owner))
            self.undos.append((label, undo))

        copy_tree_exact(
            source,
            target,
            expected,
            before_commit_hook=(
                (lambda: self.before_publish_hook(label, target))
                if self.before_publish_hook is not None
                else None
            ),
            published_owner_hook=published,
            after_publish_hook=(
                (lambda published_target, _staging: self.after_physical_hook(label, published_target))
                if self.after_physical_hook is not None
                else None
            ),
        )
        self._completed(label)

    def set_mtime(self, path: pathlib.Path, value: int, label: str) -> None:
        before = capture_transaction_node(path)
        rollback_index = len(self.undos)
        committed = copy.deepcopy(before)
        if committed.get("kind") == "file":
            committed["file"]["last_write_ns"] = value
        elif committed.get("kind") == "directory":
            committed["tree"]["root_last_write_ns"] = value
            committed_tree_payload = copy.deepcopy(committed["tree"])
            committed_tree_payload.pop("tree_hash", None)
            committed["tree"]["tree_hash"] = sha256_bytes(
                canonical_bytes(committed_tree_payload)
            )
        else:
            raise WorkflowError(f"transaction mtime target kind is invalid: {label}")

        def undo() -> None:
            claim = self.recovery_root / "r" / f"u-{rollback_index:03d}"
            claimed = claim_transaction_node(
                path,
                committed,
                claim,
                before_claim_hook=self._claim_hook(label, normalized(path)),
            )
            before_mtime = int(
                before["file"]["last_write_ns"]
                if before.get("kind") == "file"
                else before["tree"]["root_last_write_ns"]
            )
            os.utime(claimed, ns=(before_mtime, before_mtime))
            if not transaction_node_matches(claimed, before):
                raise WorkflowError(f"rollback mtime restoration was not exact: {label}")
            if os.path.lexists(path):
                raise WorkflowError(f"rollback mtime path was externally recreated; owned output retained: {label}")
            os.rename(claimed, path)
            if not transaction_node_matches(path, before):
                raise WorkflowError(f"rollback mtime restoration was not durably visible: {label}")

        self._append("restore_step_intent", {"kind": "set_mtime", "label": label})
        if self.before_publish_hook is not None:
            self.before_publish_hook(label, normalized(path))
        # Register the exact ownership-safe undo before the physical timestamp
        # mutation.  A fault immediately after os.utime must never leave an
        # untracked physical change that a later attempt could misclassify as a
        # clean rollback.  If os.utime itself fails or an external actor wins the
        # pathname, rollback fails closed and publishes the transaction latch.
        self.undos.append((label, undo))
        os.utime(path, ns=(value, value))
        if self.after_physical_hook is not None:
            self.after_physical_hook(label, normalized(path))
        if not transaction_node_matches(path, committed):
            raise WorkflowError(f"transaction mtime ownership changed after publication: {label}")
        self._completed(label)

    def commit(self) -> None:
        self.state = "physical_committed_pending_journal"
        try:
            self._append(
                "restore_transaction_commit_prepared",
                {"step_count": self.completed, "state": self.state},
            )
            self._append(
                "restore_transaction_physical_committed",
                {"step_count": self.completed, "state": "journal_confirmed"},
            )
        except Exception:
            self.state = "commit_journal_outcome_unknown"
            self._publish_recovery_state(self.state, ["commit_journal"])
            raise
        self.state = "journal_confirmed"
        self.committed = True
        self.undos.clear()

    def rollback(self) -> None:
        failures: list[str] = []
        journal_failures: list[str] = []
        try:
            self._append("restore_rollback_started", {"undo_count": len(self.undos)})
        except Exception:
            journal_failures.append("rollback_started")
        for label, undo in reversed(self.undos):
            try:
                undo()
                try:
                    self._append("restore_rollback_step", {"label": label, "status": "restored"})
                except Exception:
                    journal_failures.append(f"rollback_step:{label}")
            except Exception as exc:
                failures.append(label)
                try:
                    self._append(
                        "restore_rollback_step",
                        {
                            "label": label,
                            "status": "outcome_unknown_external_state_retained",
                            "error_type": type(exc).__name__,
                            "recovery_required": True,
                        },
                    )
                except Exception:
                    journal_failures.append(f"rollback_failure:{label}")
        self.undos.clear()
        try:
            self._append(
                "restore_rollback_complete" if not failures else "restore_rollback_failed",
                {
                    "failed_count": len(failures),
                    "journal_failure_count": len(journal_failures),
                    "recovery_required": bool(failures or journal_failures),
                },
            )
        except Exception:
            journal_failures.append("rollback_complete")
        if failures or journal_failures:
            self.state = "rollback_outcome_unknown"
            self._publish_recovery_state(self.state, [*failures, *journal_failures])
            raise WorkflowError("transaction rollback outcome_unknown; recovery evidence retained")
        self.state = "rolled_back"


def _finalize_file_operations(
    roots: Roots,
    manifest: Mapping[str, Any],
    *,
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
    derivative_save: pathlib.Path,
    derivative_preview: pathlib.Path,
    final_root: pathlib.Path,
    bridge: pathlib.Path,
    old_archive: Optional[pathlib.Path],
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = [
        {
            "kind": "move",
            "source": derivative_save,
            "target": final_root / "lineage-derivative" / derivative_save.name,
            "label": "archive_derivative_save",
        },
        {
            "kind": "move",
            "source": derivative_preview,
            "target": final_root / "lineage-derivative" / derivative_preview.name,
            "label": "archive_derivative_preview",
        },
        {
            "kind": "move",
            "source": loader_save,
            "target": final_root / "loader-before-restore" / loader_save.name,
            "label": "archive_loader_save",
        },
        {
            "kind": "move",
            "source": loader_save.with_suffix(".png"),
            "target": final_root / "loader-before-restore" / loader_save.with_suffix(".png").name,
            "label": "archive_loader_preview",
        },
        {
            "kind": "copy_file",
            "source": source_save,
            "target": loader_save,
            "label": "restore_loader_save",
        },
        {
            "kind": "copy_file",
            "source": source_save.with_suffix(".png"),
            "target": loader_save.with_suffix(".png"),
            "label": "restore_loader_preview",
        },
    ]
    for record in manifest["backup"]["configs"]:
        relative = pathlib.Path(record["relative"])
        current = roots.oni_root / relative
        evidence = final_root / "runtime-current" / "config" / relative
        label = record["relative"].replace("/", "_").replace("\\", "_")
        if current.exists():
            operations.append(
                {
                    "kind": "move",
                    "source": current,
                    "target": evidence,
                    "label": f"archive_current_{label}",
                }
            )
        if record["exists"]:
            operations.append(
                {
                    "kind": "copy_file",
                    "source": roots.environment_dir / pathlib.Path(record["backup_relative"]),
                    "target": current,
                    "label": f"restore_{label}",
                }
            )

    probe_record = manifest["backup"]["probe"]
    probe = roots.oni_root / PROBE_RELATIVE
    if probe.exists():
        operations.append(
            {
                "kind": "move",
                "source": probe,
                "target": final_root / "runtime-current" / "probe",
                "label": "archive_current_probe",
            }
        )
    if probe_record["snapshot"]["exists"]:
        operations.append(
            {
                "kind": "copy_tree",
                "source": roots.environment_dir / pathlib.Path(probe_record["backup_relative"]),
                "target": probe,
                "expected": probe_record["snapshot"],
                "label": "restore_probe",
            }
        )

    operations.append(
        {
            "kind": "move",
            "source": bridge,
            "target": roots.environment_dir / "rb",
            "label": "archive_runtime_bridge",
        }
    )
    if manifest["backup"]["old_bridge"]["snapshot"]["exists"]:
        if old_archive is None:
            raise WorkflowError("old bridge archive disappeared before transaction planning")
        operations.append(
            {
                "kind": "move",
                "source": old_archive,
                "target": bridge,
                "label": "restore_old_bridge",
            }
        )

    archive_snapshot = manifest["backup"]["bridge_archive_root"]["snapshot"]
    archive_root = roots.oni_root / BRIDGE_ARCHIVE_RELATIVE
    if archive_snapshot["exists"]:
        if not archive_root.exists():
            raise WorkflowError("preexisting bridge archive root disappeared")
        operations.append(
            {
                "kind": "set_mtime",
                "source": archive_root,
                "target": archive_root,
                "value": int(archive_snapshot["root_last_write_ns"]),
                "label": "restore_archive_root_timestamp",
            }
        )
    elif archive_root.exists():
        expected_entries = [
            entry for entry in archive_root.iterdir() if old_archive is None or not same_path(entry, old_archive)
        ]
        if expected_entries:
            raise WorkflowError("new bridge archive root contains unexpected entries")
        operations.append(
            {
                "kind": "move",
                "source": archive_root,
                "target": final_root / "runtime-current" / "empty-ai-bridge-archives",
                "label": "archive_empty_archive_root",
            }
        )
    return operations


def _execute_finalize_file_operations(
    tx: FileTransaction,
    operations: Sequence[Mapping[str, Any]],
) -> None:
    for operation in operations:
        kind = operation["kind"]
        if kind == "move":
            tx.move(operation["source"], operation["target"], operation["label"])
        elif kind == "copy_file":
            tx.copy_file(operation["source"], operation["target"], operation["label"])
        elif kind == "copy_tree":
            tx.copy_tree(
                operation["source"],
                operation["target"],
                operation["expected"],
                operation["label"],
            )
        elif kind == "set_mtime":
            tx.set_mtime(
                operation["source"],
                int(operation["value"]),
                operation["label"],
            )
        else:
            raise WorkflowError("finalize transaction plan contains an unsupported operation")


def copy_evidence_once(source: pathlib.Path, target: pathlib.Path, expected_hash: str) -> None:
    if target.exists():
        existing = capture_transaction_node(target)
        if existing.get("kind") != "file" or existing.get("file", {}).get("sha256") != expected_hash:
            raise WorkflowError("existing frozen runtime evidence has a hash conflict")
        return
    copied = copy_file_stable(source, target)
    if copied["sha256"] != expected_hash:
        raise WorkflowError("frozen runtime evidence hash mismatch")


def _parse_aware_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise WorkflowError(f"{label} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowError(f"{label} must include a UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def _runtime_config_namespace(
    args: argparse.Namespace,
    *,
    expected_environment_manifest_sha256: str,
) -> argparse.Namespace:
    """Build the exact read-only namespace consumed by the frozen config core."""

    return argparse.Namespace(
        run_id=args.run_id,
        oni_root=args.oni_root,
        evidence_root=args.evidence_root,
        boundary_report_root=args.boundary_report_root,
        environment_manifest=args.manifest,
        expected_environment_manifest_sha256=expected_environment_manifest_sha256,
        runtime_bootstrap_result=args.runtime_bootstrap_result,
        expected_runtime_bootstrap_result_sha256=(
            args.expected_runtime_bootstrap_result_sha256
        ),
        pre_boundary=args.pre_boundary,
        clone_manifest=args.clone_manifest,
        source_save=args.source_save,
        loader_save=args.loader_save,
        loader_stem=args.loader_stem,
        expected_mod_version=EXPECTED_RUNTIME_CONFIG_MOD_VERSION,
        release_dll=args.release_dll,
        expected_release_dll_sha256=args.expected_release_dll_sha256,
        game_managed_dir=args.game_managed_dir,
        expected_runtime_config_evidence_sha256=args.expected_runtime_config_evidence_sha256,
        expected_startup_binding_sha256=args.expected_startup_binding_sha256,
        test_root=args.test_root,
        process_state_fixture=args.process_state_fixture,
        confirm_live_run_id=args.confirm_live_run_id,
        apply=False,
        what_if=True,
        test_fail_after_replace=0,
        test_managed_assembly_csharp_sha256=getattr(
            args, "test_managed_assembly_csharp_sha256", None
        ),
        test_managed_assembly_csharp_firstpass_sha256=getattr(
            args, "test_managed_assembly_csharp_firstpass_sha256", None
        ),
    )


def _reject_runtime_config_recovery_records(roots: Roots) -> None:
    if not roots.environment_dir.is_dir():
        raise WorkflowError("prepared environment directory is missing")
    for candidate in roots.environment_dir.iterdir():
        if re.fullmatch(r"runtime-config-recovery-.*\.json", candidate.name, re.IGNORECASE):
            raise WorkflowError("runtime config recovery evidence blocks finalization")


def _expected_runtime_config_bindings(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    bindings = manifest.get("bindings") if isinstance(manifest.get("bindings"), Mapping) else {}
    source = bindings.get("source_save") if isinstance(bindings.get("source_save"), Mapping) else {}
    loader = bindings.get("loader_save") if isinstance(bindings.get("loader_save"), Mapping) else {}
    pre_boundary = (
        bindings.get("pre_boundary")
        if isinstance(bindings.get("pre_boundary"), Mapping)
        else {}
    )
    clone_manifest = (
        bindings.get("clone_manifest")
        if isinstance(bindings.get("clone_manifest"), Mapping)
        else {}
    )
    if (
        source.get("path_id_lower") != path_id(normalized(args.source_save), contract="lower")
        or loader.get("path_id_lower") != path_id(normalized(args.loader_save), contract="lower")
        or loader.get("stem_id") != text_id(args.loader_stem)
    ):
        raise WorkflowError("environment source/loader binding is invalid for runtime config evidence")
    return {
        "pre_boundary_sha256": pre_boundary.get("sha256"),
        "clone_manifest_sha256": clone_manifest.get("sha256"),
        "source_save": {
            "path_id_lower": source.get("path_id_lower"),
            "path_id_upper": source.get("path_id_upper"),
            "state": source.get("state"),
        },
        "loader_save": {
            "path_id_lower": loader.get("path_id_lower"),
            "path_id_upper": loader.get("path_id_upper"),
            "stem_id": loader.get("stem_id"),
            "state": loader.get("state"),
        },
    }


def _runtime_config_identity_id(identity: Sequence[int]) -> str:
    """Use the frozen config core's two-field descriptor identity contract."""

    if len(identity) < 2:
        raise WorkflowError("runtime config descriptor identity is incomplete")
    return runtime_config_core.identity_id((int(identity[0]), int(identity[1])))


def _locate_runtime_startup_binding(
    args: argparse.Namespace,
    roots: Roots,
) -> tuple[pathlib.Path, dict[str, Any], dict[str, Any]]:
    """Find exactly one externally anchored startup binding across environment custody.

    The logical publication path is fixed under ``ai-bridge``.  Finalize and
    abort move that whole tree into evidence custody, so recovery must locate
    the same inode there without weakening the external raw SHA-256 anchor.
    """

    expected_hash = require_hash(
        args.expected_startup_binding_sha256,
        "external startup binding anchor",
    )
    relative = pathlib.Path(runtime_config_core.STARTUP_BINDING_NAME)
    candidates = (
        (roots.oni_root / BRIDGE_RELATIVE / relative, roots.oni_root),
        (roots.environment_dir / "rb" / relative, roots.evidence_root),
        (roots.environment_dir / "abort" / "rb" / relative, roots.evidence_root),
    )
    matches: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for candidate, boundary in candidates:
        if not os.path.lexists(candidate):
            continue
        assert_no_reparse(candidate, boundary)
        snapshot = _stable_file_snapshot(
            candidate,
            max_bytes=MAX_JSON_BYTES,
            collect_bytes=True,
        )
        if snapshot["sha256"] == expected_hash:
            matches.append((candidate, snapshot))
    if len(matches) != 1:
        raise WorkflowError(
            "external startup binding anchor must identify exactly one current or archived file"
        )
    path, snapshot = matches[0]
    payload = snapshot.get("bytes")
    if not isinstance(payload, bytes):
        raise WorkflowError("startup binding stable bytes are unavailable")
    try:
        document = runtime_config_core.parse_json_payload(payload, "runtime startup binding")
        if payload != runtime_config_core.json_document_bytes(document):
            raise WorkflowError("runtime startup binding raw serialization is not exact")
    except runtime_config_core.ConfigError as exc:
        raise WorkflowError(f"runtime startup binding is invalid: {exc}") from exc
    return path, snapshot, document


def _runtime_bootstrap_tree_identity(
    root: pathlib.Path,
    boundary: pathlib.Path,
) -> dict[str, Any]:
    """Capture the producer's exact three-integer tree identity grammar."""

    root = normalized(root)
    boundary = normalized(boundary)
    assert_no_reparse(root, boundary)
    root_metadata = os.lstat(root)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise WorkflowError("runtime bootstrap recovery root is not a directory")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        assert_no_reparse(path, boundary)
        metadata = os.lstat(path)
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode) and int(metadata.st_nlink) == 1:
            kind = "file"
        else:
            raise WorkflowError(
                "runtime bootstrap recovery contains an unsafe filesystem node"
            )
        entries.append(
            {
                "relative": path.relative_to(root).as_posix(),
                "kind": kind,
                "identity": list(_owned_node_identity(metadata)),
            }
        )
    return {
        "exists": True,
        "root_identity": list(_owned_node_identity(root_metadata)),
        "entries": entries,
    }


def _validate_runtime_bootstrap_old_probe_recovery(
    runtime_config_roots: runtime_config_core.Roots,
    runtime_result_document: Mapping[str, Any],
    *,
    require_current_recovery_custody: bool,
) -> None:
    """Bind the result's old-Probe projection to its frozen prestate and tree.

    A completed environment finalization has legitimately moved a present old
    Probe back out of the bootstrap recovery tree.  Offline replay validation
    therefore permits that one missing present-tree state; every live
    pretransaction validation requires the exact captured content and inode
    tree.  An originally absent Probe always requires an exact ``None`` result
    projection and an empty-or-absent recovery directory.
    """

    if type(require_current_recovery_custody) is not bool:
        raise WorkflowError("runtime bootstrap recovery custody mode is invalid")
    stage_path = (
        runtime_config_roots.environment_dir
        / "runtime-bootstrap-install-stage.json"
    )
    try:
        stage, _stage_payload, _stage_state = (
            runtime_config_core.read_exact_json_document(
                stage_path,
                "finalizer runtime bootstrap install stage",
                boundary=runtime_config_roots.evidence_root,
                expected_raw_sha256=require_hash(
                    runtime_result_document.get("install_stage_sha256"),
                    "runtime bootstrap install stage raw hash",
                ),
            )
        )
    except runtime_config_core.ConfigError as exc:
        raise WorkflowError(
            f"runtime bootstrap install stage is invalid: {exc}"
        ) from exc
    # The config core historical semantic validator has already authenticated
    # the complete stage/plan/result/lock/candidate/producer graph.  Read only
    # its Probe prestate here; the environment owner adds the one physical
    # recovery-tree custody check that is specific to restore/finalize timing.
    plan = stage.get("install_plan")
    prestate = plan.get("prestate") if isinstance(plan, Mapping) else None
    probe_prestate = (
        prestate.get("probe") if isinstance(prestate, Mapping) else None
    )
    if not isinstance(probe_prestate, Mapping):
        raise WorkflowError(
            "validated runtime bootstrap stage omitted the old-Probe prestate"
        )

    snapshot = probe_prestate["snapshot"]
    old_probe_present = snapshot.get("exists") is True
    expected_relative = "recovery/old-probe" if old_probe_present else None
    if runtime_result_document.get("old_probe_recovery_relative") != expected_relative:
        raise WorkflowError(
            "runtime bootstrap old-Probe recovery projection contradicts its prestate"
        )

    recovery_root = runtime_config_roots.runtime_bootstrap_dir / "recovery"
    recovery = recovery_root / "old-probe"
    if not old_probe_present:
        if os.path.lexists(recovery_root):
            recovery_snapshot = snapshot_tree(recovery_root)
            if (
                recovery_snapshot.get("exists") is not True
                or recovery_snapshot.get("entries") != []
                or recovery_snapshot.get("file_count") != 0
                or recovery_snapshot.get("total_bytes") != 0
            ):
                raise WorkflowError(
                    "runtime bootstrap absent old-Probe has an unexpected recovery tree"
                )
        return

    if not os.path.lexists(recovery):
        if require_current_recovery_custody:
            raise WorkflowError("runtime bootstrap old-Probe recovery tree is missing")
        return
    identity_before = _runtime_bootstrap_tree_identity(
        recovery, runtime_config_roots.evidence_root
    )
    snapshot_before = snapshot_tree(recovery)
    identity_after = _runtime_bootstrap_tree_identity(
        recovery, runtime_config_roots.evidence_root
    )
    snapshot_after = snapshot_tree(recovery)
    if (
        snapshot_before != snapshot
        or snapshot_after != snapshot
        or identity_before != probe_prestate["identity"]
        or identity_after != probe_prestate["identity"]
    ):
        raise WorkflowError(
            "runtime bootstrap old-Probe recovery lost its captured snapshot/identity custody"
        )


def _runtime_config_binding_from_document(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    evidence_path: pathlib.Path,
    evidence_file_hash: str,
    evidence_identity: Sequence[int],
    document: Mapping[str, Any],
    runtime_config_roots: runtime_config_core.Roots,
    managed_assembly_binding: Mapping[str, Any],
    startup_binding_path: pathlib.Path,
    startup_binding_snapshot: Mapping[str, Any],
    startup_binding_document: Mapping[str, Any],
    *,
    require_current_recovery_custody: bool,
) -> tuple[dict[str, Any], dt.datetime, dict[str, Any]]:
    """Reconstruct the compact historical binding without consulting mutable runtime configs."""

    expected_mode = "live" if roots.live_mode else "test_fixture"
    configured_at = _parse_aware_timestamp(document.get("configured_at"), "runtime config configured_at")
    try:
        runtime_config_core.require_canonical_utc_timestamp(
            document.get("configured_at"),
            "runtime config configured_at",
        )
    except runtime_config_core.ConfigError as exc:
        raise WorkflowError(f"runtime config configured_at is invalid: {exc}") from exc
    if (
        set(document) != runtime_config_core.EVIDENCE_KEYS
        or document.get("schema") != runtime_config_core.EVIDENCE_SCHEMA
        or document.get("run_id") != roots.run_id
        or document.get("mode") != expected_mode
        or document.get("environment_manifest_sha256") != manifest_hash
        or runtime_config_core.TRANSACTION_ID_RE.fullmatch(str(document.get("transaction_id") or "")) is None
    ):
        raise WorkflowError("runtime config evidence does not bind this run/environment")
    claimed_document_hash = require_hash(
        document.get("document_hash"), "runtime config evidence document hash"
    )
    unsigned = dict(document)
    del unsigned["document_hash"]
    if claimed_document_hash != sha256_bytes(canonical_bytes(unsigned)):
        raise WorkflowError("runtime config evidence document self-hash is invalid")
    if document.get("bindings") != _expected_runtime_config_bindings(args, roots, manifest):
        raise WorkflowError("runtime config evidence source/loader binding is invalid")

    startup_intent = (
        document.get("startup_intent")
        if isinstance(document.get("startup_intent"), Mapping)
        else {}
    )
    if (
        set(startup_intent) != runtime_config_core.STARTUP_INTENT_KEYS
        or startup_intent.get("schema") != runtime_config_core.STARTUP_INTENT_SCHEMA
        or runtime_config_core.STARTUP_NONCE_RE.fullmatch(
            str(startup_intent.get("startup_nonce") or "")
        )
        is None
        or startup_intent.get("document_hash")
        != sha256_bytes(
            canonical_bytes(
                {
                    key: value
                    for key, value in startup_intent.items()
                    if key != "document_hash"
                }
            )
        )
    ):
        raise WorkflowError("runtime config startup intent is invalid")

    runtime = document.get("runtime") if isinstance(document.get("runtime"), Mapping) else {}
    if set(runtime) != runtime_config_core.RUNTIME_EVIDENCE_KEYS:
        raise WorkflowError("runtime config historical runtime field set is invalid")
    bootstrap = runtime.get("bootstrap") if isinstance(runtime.get("bootstrap"), Mapping) else {}
    policy = (
        runtime.get("initial_disabled_policy")
        if isinstance(runtime.get("initial_disabled_policy"), Mapping)
        else {}
    )
    release = runtime.get("release_dll") if isinstance(runtime.get("release_dll"), Mapping) else {}
    installed = runtime.get("installed_probe") if isinstance(runtime.get("installed_probe"), Mapping) else {}
    runtime_result_record = (
        runtime.get("runtime_bootstrap_result")
        if isinstance(runtime.get("runtime_bootstrap_result"), Mapping)
        else {}
    )
    if (
        set(bootstrap) != runtime_config_core.BOOTSTRAP_EVIDENCE_KEYS
        or set(policy) != runtime_config_core.POLICY_EVIDENCE_KEYS
        or set(release) != runtime_config_core.RELEASE_EVIDENCE_KEYS
        or set(installed) != runtime_config_core.INSTALLED_PROBE_EVIDENCE_KEYS
        or set(runtime_result_record)
        != runtime_config_core.RUNTIME_BOOTSTRAP_RESULT_EVIDENCE_KEYS
    ):
        raise WorkflowError("runtime config historical anchor shape is invalid")
    try:
        bootstrap_document = runtime_config_core.decode_frozen_json_record(
            bootstrap, "finalizer frozen bootstrap"
        )
        policy_document = runtime_config_core.decode_frozen_json_record(
            policy, "finalizer frozen initial disabled policy"
        )
        runtime_config_core.validate_disabled_policy_document(policy_document)
        (
            reconstructed_runtime_result_record,
            runtime_result_document,
        ) = runtime_config_core.validate_frozen_runtime_bootstrap_result_semantics(
            runtime_config_roots,
            manifest,
            runtime_result_record,
            bootstrap,
            active_parent_finalize_lock=_ACTIVE_PARENT_FINALIZE_LOCK.get(),
        )
        if (
            not runtime_config_core.canonical_typed_equal(
                reconstructed_runtime_result_record,
                runtime_result_record,
            )
            or not runtime_config_core.canonical_typed_equal(
                runtime_result_document,
                runtime_result_record.get("document"),
            )
        ):
            raise runtime_config_core.ConfigError(
                "historical runtime bootstrap reconstruction diverged from its exact record"
            )
    except runtime_config_core.ConfigError as exc:
        raise WorkflowError(f"runtime config embedded historical payload is invalid: {exc}") from exc
    bridge = roots.oni_root / BRIDGE_RELATIVE
    expected_release_hash = require_hash(
        args.expected_release_dll_sha256, "external Release DLL anchor"
    )
    if (
        set(bootstrap_document) != runtime_config_core.BOOTSTRAP_KEYS
        or bootstrap_document.get("schema") != runtime_config_core.BOOTSTRAP_SCHEMA
        or bootstrap_document.get("run_id") != roots.run_id
        or bootstrap_document.get("expected_mod_version") != EXPECTED_RUNTIME_CONFIG_MOD_VERSION
        or bootstrap_document.get("installed_release") is not True
        or bootstrap_document.get("action_policy_disabled") is not True
        or not isinstance(bootstrap_document.get("prepared_at"), str)
        or not bootstrap_document["prepared_at"]
        or not isinstance(bootstrap_document.get("bridge_dir"), str)
        or not same_path(bootstrap_document["bridge_dir"], bridge)
        or require_hash(bootstrap_document.get("built_dll_sha256"), "built Probe DLL hash")
        != expected_release_hash
        or require_hash(bootstrap_document.get("installed_dll_sha256"), "installed Probe DLL hash")
        != expected_release_hash
        or runtime.get("mod_version") != EXPECTED_RUNTIME_CONFIG_MOD_VERSION
        or runtime.get("probe_static_id") != runtime_config_core.PROBE_STATIC_ID
        or runtime.get("dll_four_way_identical") is not True
        or release.get("path_id_lower")
        != path_id(normalized(args.release_dll), contract="lower")
        or require_hash(release.get("external_expected_sha256"), "embedded external Release DLL anchor")
        != expected_release_hash
        or require_hash(release.get("file_sha256"), "embedded Release DLL file hash")
        != expected_release_hash
        or require_hash(installed.get("dll_sha256"), "embedded installed Probe DLL hash")
        != expected_release_hash
        or bootstrap.get("path_id_lower") != path_id(bridge / "v27-runtime-bootstrap.json", contract="lower")
        or policy.get("path_id_lower") != path_id(bridge / "action-policy.json", contract="lower")
    ):
        raise WorkflowError("runtime config bootstrap/policy/Release four-way binding is invalid")
    require_hash(installed.get("manifest_sha256"), "embedded installed Probe manifest hash")
    require_hash(installed.get("info_sha256"), "embedded installed Probe info hash")
    require_hash(installed.get("pdb_sha256"), "embedded installed Probe PDB hash")
    installed_tree_hash = require_hash(
        installed.get("tree_content_hash"),
        "embedded installed Probe tree hash",
    )
    runtime_transaction_id = runtime_result_document.get("transaction_id")
    installed_artifacts = runtime_result_document.get("installed_probe_artifacts")
    if (
        len(runtime_config_core.RUNTIME_BOOTSTRAP_RESULT_KEYS) != 38
        or len(runtime_result_document) != 38
        or set(runtime_result_document)
        != runtime_config_core.RUNTIME_BOOTSTRAP_RESULT_KEYS
        or runtime_result_document.get("schema")
        != runtime_config_core.RUNTIME_BOOTSTRAP_RESULT_SCHEMA
        or runtime_result_document.get("status") != "installed_ready"
        or runtime_result_document.get("classification") != "static_probe"
        or runtime_result_document.get("run_id") != roots.run_id
        or not isinstance(runtime_transaction_id, str)
        or runtime_config_core.RUNTIME_BOOTSTRAP_TRANSACTION_ID_RE.fullmatch(
            runtime_transaction_id
        )
        is None
        or runtime_result_document.get("environment_manifest_sha256") != manifest_hash
        or runtime_result_document.get("release_dll_sha256") != expected_release_hash
        or runtime_result_document.get("expected_mod_version")
        != EXPECTED_RUNTIME_CONFIG_MOD_VERSION
        or runtime_result_document.get("probe_tree_content_hash")
        != installed_tree_hash
        or runtime_result_document.get("physical_commit_complete") is not True
        or runtime_result_document.get("shared_lock_released") is not True
        or runtime_result_document.get("game_launched") is not False
        or not isinstance(installed_artifacts, Mapping)
        or set(installed_artifacts)
        != set(runtime_config_core.RUNTIME_BOOTSTRAP_ARTIFACT_NAMES)
    ):
        raise WorkflowError(
            "runtime config frozen bootstrap result does not bind the exact installed run"
        )
    _validate_runtime_bootstrap_old_probe_recovery(
        runtime_config_roots,
        runtime_result_document,
        require_current_recovery_custody=require_current_recovery_custody,
    )
    expected_installed_artifact_hashes = {
        "OniAiProbeMod.dll": installed["dll_sha256"],
        "OniAiProbeMod.pdb": installed["pdb_sha256"],
        "mod.yaml": installed["manifest_sha256"],
        "mod_info.yaml": installed["info_sha256"],
    }
    for name in runtime_config_core.RUNTIME_BOOTSTRAP_ARTIFACT_NAMES:
        artifact = installed_artifacts.get(name)
        if (
            not isinstance(artifact, Mapping)
            or set(artifact) != {"length", "sha256"}
            or isinstance(artifact.get("length"), bool)
            or not isinstance(artifact.get("length"), int)
            or artifact["length"] < 0
            or require_hash(
                artifact.get("sha256"),
                f"runtime bootstrap installed {name} hash",
            )
            != expected_installed_artifact_hashes[name]
        ):
            raise WorkflowError(
                "runtime config frozen bootstrap result artifact projection is invalid"
            )

    loader_state = document["bindings"]["loader_save"]["state"]
    loader_length = loader_state.get("length") if isinstance(loader_state, Mapping) else None
    if isinstance(loader_length, bool) or not isinstance(loader_length, int) or loader_length < 0:
        raise WorkflowError("runtime config startup intent loader length is invalid")
    expected_startup_intent = dict(startup_intent)
    expected_startup_intent.pop("document_hash", None)
    if expected_startup_intent != {
        "schema": runtime_config_core.STARTUP_INTENT_SCHEMA,
        "validation_run_id": roots.run_id,
        "startup_nonce": startup_intent["startup_nonce"],
        "environment_manifest_sha256": manifest_hash,
        "requested_loader_path_id_lower": document["bindings"]["loader_save"][
            "path_id_lower"
        ],
        "requested_loader_path_id_upper": document["bindings"]["loader_save"][
            "path_id_upper"
        ],
        "requested_loader_save_sha256": require_hash(
            loader_state.get("sha256"), "startup intent loader hash"
        ),
        "requested_loader_save_length": loader_length,
        "expected_game_build": runtime_config_core.EXPECTED_GAME_BUILD,
        "expected_mod_version": EXPECTED_RUNTIME_CONFIG_MOD_VERSION,
        "expected_probe_dll_sha256": require_hash(
            installed.get("dll_sha256"), "startup intent Probe DLL hash"
        ),
        "runtime_bootstrap_result_path_id_lower": runtime_result_record[
            "path_id_lower"
        ],
        "runtime_bootstrap_result_sha256": runtime_result_record["raw_sha256"],
        "runtime_bootstrap_result_hash": runtime_result_record["result_hash"],
        "runtime_bootstrap_transaction_id": runtime_result_record["transaction_id"],
        "installed_probe_tree_content_hash": runtime_result_record[
            "probe_tree_content_hash"
        ],
        "auto_resume_semantics": runtime_config_core.AUTO_RESUME_STARTUP_SEMANTICS,
    }:
        raise WorkflowError("runtime config startup intent lost its loader/runtime binding")

    startup_binding_hash = require_hash(
        args.expected_startup_binding_sha256,
        "external startup binding anchor",
    )
    if startup_binding_snapshot.get("sha256") != startup_binding_hash:
        raise WorkflowError("runtime startup binding does not match its external SHA-256 anchor")
    if (
        set(startup_binding_document) != runtime_config_core.STARTUP_BINDING_KEYS
        or startup_binding_document.get("schema")
        != runtime_config_core.STARTUP_BINDING_SCHEMA
        or startup_binding_document.get("document_hash")
        != sha256_bytes(
            canonical_bytes(
                {
                    key: value
                    for key, value in startup_binding_document.items()
                    if key != "document_hash"
                }
            )
        )
    ):
        raise WorkflowError("runtime startup binding document is invalid")
    try:
        runtime_config_core.require_canonical_utc_timestamp(
            startup_binding_document.get("created_at"),
            "runtime startup binding timestamp",
        )
    except runtime_config_core.ConfigError as exc:
        raise WorkflowError(f"runtime startup binding timestamp is invalid: {exc}") from exc
    expected_startup_binding = {
        "schema": runtime_config_core.STARTUP_BINDING_SCHEMA,
        "validation_run_id": roots.run_id,
        "startup_nonce": startup_intent["startup_nonce"],
        "startup_intent_hash": startup_intent["document_hash"],
        "runtime_config_evidence_sha256": evidence_file_hash,
        "environment_manifest_sha256": manifest_hash,
        "expected_game_build": startup_intent["expected_game_build"],
        "expected_mod_version": startup_intent["expected_mod_version"],
        "expected_probe_dll_sha256": startup_intent["expected_probe_dll_sha256"],
        "runtime_bootstrap_result_path_id_lower": startup_intent[
            "runtime_bootstrap_result_path_id_lower"
        ],
        "runtime_bootstrap_result_sha256": startup_intent[
            "runtime_bootstrap_result_sha256"
        ],
        "runtime_bootstrap_result_hash": startup_intent[
            "runtime_bootstrap_result_hash"
        ],
        "runtime_bootstrap_transaction_id": startup_intent[
            "runtime_bootstrap_transaction_id"
        ],
        "installed_probe_tree_content_hash": startup_intent[
            "installed_probe_tree_content_hash"
        ],
        "requested_loader_path_id_upper": startup_intent[
            "requested_loader_path_id_upper"
        ],
        "requested_loader_save_sha256": startup_intent[
            "requested_loader_save_sha256"
        ],
        "requested_loader_save_length": startup_intent[
            "requested_loader_save_length"
        ],
        "created_at": document["configured_at"],
    }
    sealed_expected_startup_binding = dict(expected_startup_binding)
    sealed_expected_startup_binding["document_hash"] = sha256_bytes(
        canonical_bytes(expected_startup_binding)
    )
    if dict(startup_binding_document) != sealed_expected_startup_binding:
        raise WorkflowError("runtime startup binding cross-link is invalid")

    configs = document.get("configs") if isinstance(document.get("configs"), Mapping) else {}
    if set(configs) != set(runtime_config_core.TARGET_CONFIG_RELATIVES):
        raise WorkflowError("runtime config desired-config path set is invalid")
    desired_configs: dict[str, str] = {}
    manifest_backup = (
        manifest.get("backup") if isinstance(manifest.get("backup"), Mapping) else {}
    )
    manifest_config_records = (
        manifest_backup.get("configs")
        if isinstance(manifest_backup.get("configs"), list)
        else []
    )
    manifest_configs = {
        str(item.get("relative")): item
        for item in manifest_config_records
        if isinstance(item, Mapping)
    }
    for relative in runtime_config_core.TARGET_CONFIG_RELATIVES:
        record = configs.get(relative) if isinstance(configs.get(relative), Mapping) else {}
        after = record.get("after") if isinstance(record.get("after"), Mapping) else {}
        expected_path = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        original = manifest_configs.get(relative, {})
        after_hash = require_hash(record.get("after_bytes_sha256"), f"{relative} desired hash")
        if (
            set(record) != {"relative", "path_id_lower", "before", "after", "after_bytes_sha256"}
            or set(after) != {"length", "last_write_ns", "sha256"}
            or record.get("relative") != relative
            or record.get("path_id_lower") != path_id(expected_path, contract="lower")
            or record.get("before") != original.get("state")
            or after.get("sha256") != after_hash
            or isinstance(after.get("length"), bool)
            or not isinstance(after.get("length"), int)
            or after["length"] < 0
            or isinstance(after.get("last_write_ns"), bool)
            or not isinstance(after.get("last_write_ns"), int)
            or after["last_write_ns"] < 0
        ):
            raise WorkflowError("runtime config desired-config record is invalid")
        desired_configs[relative] = after_hash
    mod_policy = (
        document.get("mod_policy") if isinstance(document.get("mod_policy"), Mapping) else {}
    )
    expected_loader_policy = {
        "save_filename_key_count": 1,
        "auto_resume_save_file_key_count": 1,
        "save_filename_key_exact_loader": True,
        "auto_resume_save_file_exact_loader": True,
        "loader_path_id_lower": path_id(normalized(args.loader_save), contract="lower"),
        "points_only_to_loader_clone": True,
        "source_continue_disabled": True,
        "built_in_semantics": runtime_config_core.bound_builtin_auto_resume_semantics(
            managed_assembly_binding
        ),
        "expected_one_shot_consumption": True,
    }
    if document.get("loader_policy") != expected_loader_policy or mod_policy.get(
        "enabled_static_ids"
    ) != [runtime_config_core.PROBE_STATIC_ID] or (
        mod_policy.get("all_other_mods_disabled") is not True
    ) or document.get("restoration") != {
        "owner": "environment_finalizer",
        "original_config_snapshot_bound": True,
    }:
        raise WorkflowError("runtime config loader/mod/restoration policy is invalid")

    verification_without_hash: dict[str, Any] = {
        "schema": runtime_config_core.VERIFY_RESULT_SCHEMA,
        "status": "passed",
        "run_id": roots.run_id,
        "environment_manifest_sha256": manifest_hash,
        "runtime_config_evidence_path_id": path_id(evidence_path, contract="lower"),
        "runtime_config_evidence_sha256": evidence_file_hash,
        "runtime_config_evidence_identity_id": _runtime_config_identity_id(
            evidence_identity
        ),
        "startup_binding_sha256": startup_binding_hash,
        "startup_binding_path_id": path_id(
            roots.oni_root
            / BRIDGE_RELATIVE
            / runtime_config_core.STARTUP_BINDING_NAME,
            contract="lower",
        ),
        "startup_binding_identity_id": _runtime_config_identity_id(
            startup_binding_snapshot["identity"]
        ),
        "startup_nonce": startup_intent["startup_nonce"],
        "startup_intent_hash": startup_intent["document_hash"],
        "requested_loader_path_id_lower": startup_intent[
            "requested_loader_path_id_lower"
        ],
        "requested_loader_path_id_upper": startup_intent[
            "requested_loader_path_id_upper"
        ],
        "requested_loader_save_sha256": startup_intent[
            "requested_loader_save_sha256"
        ],
        "requested_loader_save_length": startup_intent[
            "requested_loader_save_length"
        ],
        "expected_game_build": startup_intent["expected_game_build"],
        "expected_mod_version": startup_intent["expected_mod_version"],
        "expected_probe_dll_sha256": startup_intent[
            "expected_probe_dll_sha256"
        ],
        "runtime_bootstrap_result_path_id_lower": startup_intent[
            "runtime_bootstrap_result_path_id_lower"
        ],
        "runtime_bootstrap_result_sha256": startup_intent[
            "runtime_bootstrap_result_sha256"
        ],
        "runtime_bootstrap_result_hash": startup_intent[
            "runtime_bootstrap_result_hash"
        ],
        "runtime_bootstrap_transaction_id": startup_intent[
            "runtime_bootstrap_transaction_id"
        ],
        "installed_probe_tree_content_hash": startup_intent[
            "installed_probe_tree_content_hash"
        ],
        "kplayerprefs_sha256": desired_configs["kplayerprefs.yaml"],
        "mods_json_sha256": desired_configs["mods/mods.json"],
        "loader_clone_only": True,
        "loader_policy": expected_loader_policy,
        "enabled_static_ids": [runtime_config_core.PROBE_STATIC_ID],
        "all_other_mods_disabled": True,
        "historical_validation": True,
        "current_runtime_configs_required": False,
        "expected_release_dll_sha256": expected_release_hash,
        "bootstrap_raw_sha256": bootstrap["raw_sha256"],
        "initial_policy_raw_sha256": policy["raw_sha256"],
    }
    verification = dict(verification_without_hash)
    verification["report_hash"] = sha256_bytes(canonical_bytes(verification_without_hash))
    binding = {
        "schema": RUNTIME_CONFIG_BINDING_SCHEMA,
        "source": {
            "path_id_lower": path_id(evidence_path, contract="lower"),
            "raw_sha256": evidence_file_hash,
            "identity_id": _runtime_config_identity_id(evidence_identity),
            "document_hash": claimed_document_hash,
            "configured_at": document["configured_at"],
            "transaction_id": document["transaction_id"],
        },
        "environment_manifest_sha256": manifest_hash,
        "source_save": {
            "path_id_lower": document["bindings"]["source_save"]["path_id_lower"],
            "raw_sha256": document["bindings"]["source_save"]["state"]["sha256"],
        },
        "loader_save": {
            "path_id_lower": document["bindings"]["loader_save"]["path_id_lower"],
            "stem_id": document["bindings"]["loader_save"]["stem_id"],
        },
        "probe": {
            "static_id": runtime_config_core.PROBE_STATIC_ID,
            "mod_version": EXPECTED_RUNTIME_CONFIG_MOD_VERSION,
            "installed_dll_sha256": installed["dll_sha256"],
        },
        "bootstrap": {
            "path_id_lower": bootstrap["path_id_lower"],
            "raw_sha256": bootstrap["raw_sha256"],
            "document_hash": bootstrap["document_hash"],
        },
        "initial_disabled_policy": {
            "path_id_lower": policy["path_id_lower"],
            "raw_sha256": policy["raw_sha256"],
            "document_hash": policy["document_hash"],
        },
        "release_dll": {
            "path_id_lower": release["path_id_lower"],
            "external_expected_sha256": expected_release_hash,
            "file_sha256": release["file_sha256"],
        },
        "loader_policy": expected_loader_policy,
        "startup_intent": {
            "startup_nonce": startup_intent["startup_nonce"],
            "document_hash": startup_intent["document_hash"],
            "requested_loader_path_id_lower": startup_intent[
                "requested_loader_path_id_lower"
            ],
            "requested_loader_path_id_upper": startup_intent[
                "requested_loader_path_id_upper"
            ],
            "requested_loader_save_sha256": startup_intent[
                "requested_loader_save_sha256"
            ],
            "requested_loader_save_length": startup_intent[
                "requested_loader_save_length"
            ],
            "expected_game_build": startup_intent["expected_game_build"],
            "expected_mod_version": startup_intent["expected_mod_version"],
            "expected_probe_dll_sha256": startup_intent[
                "expected_probe_dll_sha256"
            ],
            "runtime_bootstrap_result_path_id_lower": startup_intent[
                "runtime_bootstrap_result_path_id_lower"
            ],
            "runtime_bootstrap_result_sha256": startup_intent[
                "runtime_bootstrap_result_sha256"
            ],
            "runtime_bootstrap_result_hash": startup_intent[
                "runtime_bootstrap_result_hash"
            ],
            "runtime_bootstrap_transaction_id": startup_intent[
                "runtime_bootstrap_transaction_id"
            ],
            "installed_probe_tree_content_hash": startup_intent[
                "installed_probe_tree_content_hash"
            ],
            "auto_resume_semantics": startup_intent["auto_resume_semantics"],
        },
        "startup_binding": {
            "path_id_lower": path_id(
                roots.oni_root
                / BRIDGE_RELATIVE
                / runtime_config_core.STARTUP_BINDING_NAME,
                contract="lower",
            ),
            "external_expected_sha256": startup_binding_hash,
            "file_sha256": startup_binding_snapshot["sha256"],
            "identity_id": _runtime_config_identity_id(
                startup_binding_snapshot["identity"]
            ),
            "document_hash": startup_binding_document["document_hash"],
            "created_at": startup_binding_document["created_at"],
        },
        "managed_assembly_binding": dict(managed_assembly_binding),
        "desired_configs": desired_configs,
        "verification_report_hash": verification["report_hash"],
    }
    if set(binding) != RUNTIME_CONFIG_BINDING_KEYS:
        raise WorkflowError("runtime config compact binding field set is invalid")
    return binding, configured_at, verification


def validate_runtime_config_evidence(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    *,
    verify_current_installation: bool,
) -> RuntimeConfigPreflight:
    _reject_runtime_config_recovery_records(roots)
    evidence_path = normalized(args.runtime_config_evidence)
    expected_path = roots.environment_dir / RUNTIME_CONFIG_EVIDENCE_NAME
    if not same_path(evidence_path, expected_path):
        raise WorkflowError("runtime config evidence is not at its unique environment path")
    assert_no_reparse(evidence_path, roots.evidence_root)
    expected_evidence_hash = require_hash(
        args.expected_runtime_config_evidence_sha256,
        "external runtime config evidence anchor",
    )
    namespace = _runtime_config_namespace(
        args,
        expected_environment_manifest_sha256=manifest_hash,
    )
    try:
        # This read-only root resolver is also the single authority for the exact
        # live/TestRoot Release DLL path and its external SHA-256 anchor.
        runtime_config_roots = runtime_config_core.resolve_roots(namespace)
        current_managed_assembly_binding = runtime_config_core.capture_managed_assembly_binding(
            runtime_config_roots
        )
    except (runtime_config_core.ConfigError, OSError) as exc:
        raise WorkflowError(f"runtime config Release/root binding is invalid: {exc}") from exc
    before = _stable_file_snapshot(evidence_path, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
    if before["sha256"] != expected_evidence_hash:
        raise WorkflowError("runtime config evidence does not match its external SHA-256 anchor")
    (
        startup_binding_path,
        startup_binding_before,
        startup_binding_document,
    ) = _locate_runtime_startup_binding(args, roots)
    core_result: Optional[dict[str, Any]] = None
    if verify_current_installation:
        active_parent_finalize_lock = _ACTIVE_PARENT_FINALIZE_LOCK.get()
        try:
            if active_parent_finalize_lock is not None:
                _require_current_active_parent_finalize_lock(
                    roots,
                    active_parent_finalize_lock,
                )
            try:
                core_result = runtime_config_core.verify_evidence(
                    namespace,
                    active_parent_finalize_lock=active_parent_finalize_lock,
                )
            finally:
                if active_parent_finalize_lock is not None:
                    _require_current_active_parent_finalize_lock(
                        roots,
                        active_parent_finalize_lock,
                    )
        except runtime_config_core.ConfigError as exc:
            raise WorkflowError(f"runtime config historical verification failed: {exc}") from exc
    after = _stable_file_snapshot(evidence_path, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
    startup_binding_after = _stable_file_snapshot(
        startup_binding_path,
        max_bytes=MAX_JSON_BYTES,
        collect_bytes=True,
    )
    if (
        before["identity"] != after["identity"]
        or before["sha256"] != after["sha256"]
        or before["bytes"] != after["bytes"]
    ):
        raise WorkflowError("runtime config evidence changed across historical verification")
    if (
        startup_binding_before["identity"] != startup_binding_after["identity"]
        or startup_binding_before["sha256"] != startup_binding_after["sha256"]
        or startup_binding_before["bytes"] != startup_binding_after["bytes"]
    ):
        raise WorkflowError("runtime startup binding changed across historical verification")
    payload = after["bytes"]
    if not isinstance(payload, bytes):
        raise WorkflowError("runtime config evidence stable bytes are unavailable")
    try:
        document = runtime_config_core.parse_json_payload(payload, "runtime config evidence")
        if payload != runtime_config_core.json_document_bytes(document):
            raise WorkflowError("runtime config evidence raw serialization is not exact")
        binding, configured_at, reconstructed_verification = _runtime_config_binding_from_document(
            args,
            roots,
            manifest,
            manifest_hash,
            evidence_path,
            expected_evidence_hash,
            after["identity"],
            document,
            runtime_config_roots,
            current_managed_assembly_binding,
            startup_binding_path,
            startup_binding_after,
            startup_binding_document,
            require_current_recovery_custody=verify_current_installation,
        )
    except runtime_config_core.ConfigError as exc:
        raise WorkflowError(f"runtime config evidence is invalid: {exc}") from exc
    if core_result is not None and core_result != reconstructed_verification:
        raise WorkflowError("runtime config core result diverged from offline historical reconstruction")
    return RuntimeConfigPreflight(
        path=evidence_path,
        file_sha256=expected_evidence_hash,
        document=document,
        binding=binding,
        binding_hash=sha256_bytes(canonical_bytes(binding)),
        configured_at=configured_at,
        verification=core_result,
        startup_binding_path=startup_binding_path,
        startup_binding_file_sha256=startup_binding_after["sha256"],
    )


def verify_runtime_config_frozen_copy(
    roots: Roots,
    preflight: RuntimeConfigPreflight,
    *,
    required: bool,
) -> Optional[dict[str, Any]]:
    target = roots.environment_dir / "finalization" / pathlib.Path(
        *RUNTIME_CONFIG_FROZEN_RELATIVE.parts
    )
    if not os.path.lexists(target):
        if required:
            raise WorkflowError("frozen runtime config evidence is missing")
        return None
    snapshot = _stable_file_snapshot(target, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
    source = _stable_file_snapshot(preflight.path, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
    if (
        snapshot["sha256"] != preflight.file_sha256
        or source["sha256"] != preflight.file_sha256
        or snapshot["bytes"] != source["bytes"]
    ):
        raise WorkflowError("frozen runtime config evidence conflicts with the external anchor")
    return {
        "path_id_lower": path_id(target, contract="lower"),
        "file_sha256": preflight.file_sha256,
    }


def _startup_bundle_custody_snapshot(
    bundle: pathlib.Path,
    boundary: pathlib.Path,
) -> dict[str, Any]:
    """Capture descriptor identities and bytes for the strict six-file bundle."""

    bundle = normalized(bundle)
    boundary = normalized(boundary)
    assert_no_reparse(bundle, boundary)
    try:
        directory_before = os.lstat(bundle)
        if (
            not stat.S_ISDIR(directory_before.st_mode)
            or stat.S_ISLNK(directory_before.st_mode)
            or bool(getattr(directory_before, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)
        ):
            raise WorkflowError("startup consumption bundle root is unsafe")
        expected_names = set(startup_contract.ARTIFACT_FILES.values()) | {
            startup_contract.REPORT_FILE
        }
        actual_names = {entry.name for entry in bundle.iterdir()}
    except OSError as exc:
        raise WorkflowError("startup consumption bundle is unreadable") from exc
    if actual_names != expected_names:
        raise WorkflowError("startup consumption bundle entry set is not exact")

    entries: dict[str, dict[str, Any]] = {}
    for name in sorted(expected_names, key=str.casefold):
        snapshot = _stable_file_snapshot(bundle / name, max_bytes=MAX_TREE_BYTES)
        entries[name] = {
            "identity": list(snapshot["identity"]),
            "length": int(snapshot["length"]),
            "last_write_ns": int(snapshot["last_write_ns"]),
            "sha256": str(snapshot["sha256"]),
        }
    try:
        directory_after = os.lstat(bundle)
    except OSError as exc:
        raise WorkflowError("startup consumption bundle disappeared during custody capture") from exc
    if (
        _owned_node_identity(directory_after) != _owned_node_identity(directory_before)
        or int(directory_after.st_mtime_ns) != int(directory_before.st_mtime_ns)
    ):
        raise WorkflowError("startup consumption bundle directory changed during custody capture")
    custody: dict[str, Any] = {
        "directory_identity": list(_owned_node_identity(directory_before)),
        "directory_last_write_ns": int(directory_before.st_mtime_ns),
        "entry_count": len(entries),
        "entries": entries,
    }
    return custody


def _verify_startup_bundle_custody(
    bundle: pathlib.Path,
    boundary: pathlib.Path,
    expected: Mapping[str, Any],
    label: str,
) -> None:
    if _startup_bundle_custody_snapshot(bundle, boundary) != dict(expected):
        raise WorkflowError(f"{label} startup consumption bundle custody changed")


def _startup_certificate_projection(
    shutdown_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    document = shutdown_evidence.get("document")
    certificate = (
        document.get("startup_consumption_certificate")
        if isinstance(document, Mapping)
        else None
    )
    if not isinstance(certificate, Mapping) or set(certificate) != shutdown_contract.STARTUP_CERTIFICATE_KEYS:
        raise WorkflowError("normal shutdown evidence has no exact startup certificate projection")
    return copy.deepcopy(dict(certificate))


def _cross_bind_startup_certificate(
    certificate: Mapping[str, Any],
    *,
    roots: Roots,
    runtime_config: RuntimeConfigPreflight,
    bundle_path: pathlib.Path,
    expected_evidence_hash: str,
    expected_validation_process_id: int,
    custody: Mapping[str, Any],
) -> str:
    if (
        certificate.get("schema") != startup_contract.EVIDENCE_SCHEMA
        or certificate.get("status") != "passed"
        or certificate.get("validation_run_id") != roots.run_id
        or certificate.get("evidence_hash") != expected_evidence_hash
    ):
        raise WorkflowError("startup certificate top-level projection is invalid")
    bundle = certificate.get("bundle") if isinstance(certificate.get("bundle"), Mapping) else {}
    replay = certificate.get("replay") if isinstance(certificate.get("replay"), Mapping) else {}
    binding = certificate.get("binding") if isinstance(certificate.get("binding"), Mapping) else {}
    process = certificate.get("process") if isinstance(certificate.get("process"), Mapping) else {}
    session = certificate.get("session") if isinstance(certificate.get("session"), Mapping) else {}
    loader = certificate.get("loader") if isinstance(certificate.get("loader"), Mapping) else {}
    if (
        set(bundle) != shutdown_contract.STARTUP_CERTIFICATE_BUNDLE_KEYS
        or set(replay) != shutdown_contract.STARTUP_CERTIFICATE_REPLAY_KEYS
        or set(binding) != shutdown_contract.STARTUP_CERTIFICATE_BINDING_KEYS
        or set(process) != startup_contract.PROCESS_EVIDENCE_KEYS
        or set(session) != shutdown_contract.STARTUP_CERTIFICATE_SESSION_KEYS
        or set(loader) != shutdown_contract.STARTUP_CERTIFICATE_LOADER_KEYS
    ):
        raise WorkflowError("startup certificate nested projection is invalid")
    if (
        bundle.get("directory_name") != shutdown_contract.STARTUP_BUNDLE_DIRECTORY
        or bundle.get("path_id_lower") != path_id(bundle_path, contract="lower")
        or bundle.get("report_file") != startup_contract.REPORT_FILE
        or bundle.get("entry_count") != len(startup_contract.ARTIFACT_FILES) + 1
        or bundle.get("artifact_count") != len(startup_contract.ARTIFACT_FILES)
        or replay
        != {
            "schema": startup_contract.REPLAY_VERIFICATION_SCHEMA,
            "status": "passed",
            "verification_mode": "offline_replay",
            "fail_closed": True,
        }
    ):
        raise WorkflowError("startup certificate bundle/replay projection is invalid")
    entries = custody.get("entries") if isinstance(custody.get("entries"), Mapping) else {}
    report_entry = entries.get(startup_contract.REPORT_FILE) if isinstance(entries, Mapping) else None
    prefix_name = startup_contract.ARTIFACT_FILES["session_archive_prefix"]
    prefix_entry = entries.get(prefix_name) if isinstance(entries, Mapping) else None
    if (
        not isinstance(report_entry, Mapping)
        or not isinstance(prefix_entry, Mapping)
        or bundle.get("report_file_sha256") != report_entry.get("sha256")
        or session.get("prefix_file_sha256") != prefix_entry.get("sha256")
        or session.get("prefix_byte_length") != prefix_entry.get("length")
    ):
        raise WorkflowError("startup certificate raw bundle/prefix custody is invalid")

    startup_intent = runtime_config.binding.get("startup_intent")
    startup_binding = runtime_config.binding.get("startup_binding")
    if not isinstance(startup_intent, Mapping) or not isinstance(startup_binding, Mapping):
        raise WorkflowError("runtime config startup anchors are incomplete")
    expected_binding = {
        "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
        "startup_intent_hash": startup_intent.get("document_hash"),
        "runtime_config_evidence_sha256": runtime_config.file_sha256,
        "startup_binding_status": "verified",
        "requested_save_path_id": startup_intent.get("requested_loader_path_id_upper"),
        "active_save_path_id": startup_intent.get("requested_loader_path_id_upper"),
    }
    expected_loader = {
        "initial_save_length": startup_intent.get("requested_loader_save_length"),
        "expected_game_build": startup_intent.get("expected_game_build"),
        "expected_mod_version": startup_intent.get("expected_mod_version"),
        "expected_probe_dll_sha256": startup_intent.get("expected_probe_dll_sha256"),
    }
    if (
        dict(binding) != expected_binding
        or dict(loader) != expected_loader
        or session.get("initial_save_sha256")
        != startup_intent.get("requested_loader_save_sha256")
        or process.get("process_id") != expected_validation_process_id
        or startup_binding.get("file_sha256") != runtime_config.startup_binding_file_sha256
    ):
        raise WorkflowError("startup certificate is not exactly cross-bound to runtime config/PID")
    try:
        return contract_canonical_hash(dict(certificate))
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("startup certificate projection is not canonically hashable") from exc


def _shutdown_lineage_origin(
    document: Mapping[str, Any],
    roots: Roots,
    *,
    certificate: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    lineage = document.get("lineage") if isinstance(document.get("lineage"), Mapping) else {}
    origin = lineage.get("origin") if isinstance(lineage.get("origin"), Mapping) else {}
    activation = (
        lineage.get("activation") if isinstance(lineage.get("activation"), Mapping) else {}
    )
    checkpoints = lineage.get("checkpoints") if isinstance(lineage.get("checkpoints"), list) else []
    final_tip = document.get("final_tip") if isinstance(document.get("final_tip"), Mapping) else {}
    if (
        set(lineage) != SHUTDOWN_LINEAGE_KEYS
        or set(origin) != shutdown_contract.LINEAGE_SNAPSHOT_KEYS
        or set(activation) != SHUTDOWN_ACTIVATION_KEYS
        or not checkpoints
        or any(
            not isinstance(checkpoint, Mapping)
            or set(checkpoint) != SHUTDOWN_CHECKPOINT_KEYS
            for checkpoint in checkpoints
        )
        or lineage.get("test_run_id") != roots.run_id
    ):
        raise WorkflowError("normal shutdown evidence lineage projection is invalid")
    origin_session = origin.get("session_id")
    activation_session = activation.get("reload_session_id")
    final_session = lineage.get("final_session_id")
    if (
        not isinstance(origin_session, str)
        or not origin_session
        or activation.get("source_session_id") != origin_session
        or activation.get("source_save_id_hash") != origin.get("save_id_hash")
        or not isinstance(activation_session, str)
        or not activation_session
        or activation_session == origin_session
    ):
        raise WorkflowError("normal shutdown activation projection is invalid")
    prior_session = activation_session
    prior_save = activation.get("reload_save_id_hash")
    observed_sessions = [origin_session, activation_session]
    for expected_index, checkpoint in enumerate(checkpoints, start=1):
        if (
            checkpoint.get("index") != expected_index
            or checkpoint.get("before_session_id") != prior_session
            or checkpoint.get("before_save_id_hash") != prior_save
            or checkpoint.get("reload_session_id") == prior_session
            or checkpoint.get("reload_save_id_hash") == prior_save
        ):
            raise WorkflowError("normal shutdown checkpoint lineage projection is invalid")
        prior_session = checkpoint.get("reload_session_id")
        prior_save = checkpoint.get("reload_save_id_hash")
        observed_sessions.append(prior_session)
    if (
        len(set(observed_sessions)) != len(observed_sessions)
        or final_session != prior_session
        or final_tip.get("session_id") != final_session
        or final_tip.get("save_id_hash") != prior_save
        or final_session == origin_session
    ):
        raise WorkflowError("normal shutdown origin/final lineage is not a strict session/save chain")
    if certificate is not None:
        session = certificate.get("session") if isinstance(certificate.get("session"), Mapping) else {}
        if (
            session.get("session_id") != origin_session
            or session.get("initial_save_sha256") != origin.get("save_id_hash")
        ):
            raise WorkflowError("startup certificate is not bound to the lineage origin")
    return copy.deepcopy(dict(origin))


def _shutdown_runtime_file_custody(
    path: pathlib.Path,
    bridge: pathlib.Path,
    *,
    expected_sha256: Any,
    expected_length: Optional[Any],
    label: str,
) -> dict[str, Any]:
    path = normalized(path)
    bridge = normalized(bridge)
    if not path_within(path, bridge):
        raise WorkflowError(f"{label} escaped the runtime bridge")
    relative = path.relative_to(bridge).as_posix()
    validate_relative_path(relative, label)
    snapshot = _stable_file_snapshot(path, max_bytes=shutdown_contract.MAX_LOG_BYTES)
    if snapshot["sha256"] != require_hash(expected_sha256, f"{label} hash"):
        raise WorkflowError(f"{label} conflicts with normal shutdown evidence")
    if expected_length is not None and snapshot["length"] != expected_length:
        raise WorkflowError(f"{label} length conflicts with normal shutdown evidence")
    return {
        "relative": relative,
        "logical_path_id_lower": path_id(path, contract="lower"),
        "identity": list(snapshot["identity"]),
        "length": int(snapshot["length"]),
        "last_write_ns": int(snapshot["last_write_ns"]),
        "sha256": str(snapshot["sha256"]),
    }


def capture_shutdown_runtime_custody(
    roots: Roots,
    bridge: pathlib.Path,
    shutdown_evidence: Mapping[str, Any],
    certificate: Mapping[str, Any],
) -> dict[str, Any]:
    document = shutdown_evidence.get("document")
    if not isinstance(document, Mapping):
        raise WorkflowError("normal shutdown evidence document is missing")
    origin = _shutdown_lineage_origin(document, roots, certificate=certificate)
    lineage = document["lineage"]
    origin_archive = lineage.get("origin_archive") if isinstance(lineage, Mapping) else None
    activation_archive = (
        lineage.get("activation_reload_archive") if isinstance(lineage, Mapping) else None
    )
    source = document.get("source")
    if (
        not isinstance(origin_archive, Mapping)
        or not isinstance(activation_archive, Mapping)
        or not isinstance(source, Mapping)
    ):
        raise WorkflowError("normal shutdown evidence runtime sources are incomplete")
    origin_relative = validate_relative_path(
        str(origin_archive.get("relative_path") or ""),
        "startup origin archive path",
    )
    origin_path = normalized(bridge).joinpath(*origin_relative.parts)
    activation_relative = validate_relative_path(
        str(activation_archive.get("relative_path") or ""),
        "activation reload archive path",
    )
    activation_path = normalized(bridge).joinpath(*activation_relative.parts)
    lineage_path = normalized(pathlib.Path(str(lineage.get("path") or "")))
    source_path = normalized(pathlib.Path(str(source.get("path") or "")))
    lineage_record = _shutdown_runtime_file_custody(
        lineage_path,
        bridge,
        expected_sha256=lineage.get("file_sha256"),
        expected_length=None,
        label="save lineage manifest",
    )
    origin_record = _shutdown_runtime_file_custody(
        origin_path,
        bridge,
        expected_sha256=origin_archive.get("file_sha256"),
        expected_length=origin_archive.get("byte_length"),
        label="startup origin archive",
    )
    source_record = _shutdown_runtime_file_custody(
        source_path,
        bridge,
        expected_sha256=source.get("file_sha256"),
        expected_length=source.get("byte_length"),
        label="final shutdown event log",
    )
    activation_record = _shutdown_runtime_file_custody(
        activation_path,
        bridge,
        expected_sha256=activation_archive.get("file_sha256"),
        expected_length=activation_archive.get("byte_length"),
        label="activation reload archive",
    )
    if (
        origin_archive.get("path_id_lower") != origin_record["logical_path_id_lower"]
        or activation_archive.get("path_id_lower")
        != activation_record["logical_path_id_lower"]
        or len(
            {
                origin_record["relative"],
                activation_record["relative"],
                lineage_record["relative"],
                source_record["relative"],
            }
        )
        != 4
    ):
        raise WorkflowError("normal shutdown runtime source path projections are invalid")
    payload: dict[str, Any] = {
        "schema": SHUTDOWN_RUNTIME_CUSTODY_SCHEMA,
        "lineage_origin": origin,
        "lineage_origin_hash": contract_canonical_hash(origin),
        "final_session_id": lineage["final_session_id"],
        "lineage_manifest": lineage_record,
        "origin_archive": origin_record,
        "activation_reload_archive": activation_record,
        "final_event_log": source_record,
    }
    payload["custody_hash"] = contract_canonical_hash(payload)
    return payload


def _reject_shutdown_runtime_duplicate_custody(
    custody: Mapping[str, Any],
    other_bridge: Optional[pathlib.Path],
    *,
    label: str,
) -> None:
    """Forbid a second executor-visible copy of either trusted runtime log."""

    if other_bridge is None:
        return
    bridge = normalized(other_bridge)
    if not os.path.lexists(bridge):
        return
    if is_reparse(bridge) or not bridge.is_dir():
        raise WorkflowError(f"{label} root is unsafe")
    for key in (
        "lineage_manifest",
        "origin_archive",
        "activation_reload_archive",
        "final_event_log",
    ):
        record = custody.get(key)
        if not isinstance(record, Mapping) or set(record) != SHUTDOWN_RUNTIME_FILE_CUSTODY_KEYS:
            raise WorkflowError(f"{label} {key} custody is malformed")
        relative = validate_relative_path(str(record.get("relative") or ""), key)
        current = bridge
        for part in relative.parts:
            current = current / part
            if not os.path.lexists(current):
                break
            if is_reparse(current):
                raise WorkflowError(f"{label} {key} ancestry is unsafe")
        else:
            raise WorkflowError(f"{label} contains duplicate {key} custody")


def _verify_committed_shutdown_runtime_custody(
    roots: Roots,
    runtime_binding: Mapping[str, Any],
    document: Mapping[str, Any],
    certificate: Mapping[str, Any],
    startup_replay: Mapping[str, Any],
) -> None:
    custody = runtime_binding.get("shutdown_runtime_custody")
    if not isinstance(custody, Mapping) or set(custody) != SHUTDOWN_RUNTIME_CUSTODY_KEYS:
        raise WorkflowError("committed shutdown runtime custody is missing or malformed")
    unsigned = copy.deepcopy(dict(custody))
    claimed_hash = unsigned.pop("custody_hash", None)
    origin = _shutdown_lineage_origin(document, roots, certificate=certificate)
    lineage = document["lineage"]
    if (
        claimed_hash != contract_canonical_hash(unsigned)
        or custody.get("schema") != SHUTDOWN_RUNTIME_CUSTODY_SCHEMA
        or custody.get("lineage_origin") != origin
        or custody.get("lineage_origin_hash") != contract_canonical_hash(origin)
        or custody.get("final_session_id") != lineage.get("final_session_id")
    ):
        raise WorkflowError("committed shutdown runtime custody lost its lineage binding")
    bridge_archive = roots.environment_dir / "rb"
    logical_bridge = roots.oni_root / BRIDGE_RELATIVE
    _reject_shutdown_runtime_duplicate_custody(
        custody,
        logical_bridge,
        label="restored logical bridge",
    )
    snapshots: dict[str, dict[str, Any]] = {}
    for key in (
        "lineage_manifest",
        "origin_archive",
        "activation_reload_archive",
        "final_event_log",
    ):
        record = custody.get(key)
        if not isinstance(record, Mapping) or set(record) != SHUTDOWN_RUNTIME_FILE_CUSTODY_KEYS:
            raise WorkflowError(f"committed {key} custody is malformed")
        relative = validate_relative_path(str(record.get("relative") or ""), key)
        archived = normalized(bridge_archive.joinpath(*relative.parts))
        logical = normalized(logical_bridge.joinpath(*relative.parts))
        if record.get("logical_path_id_lower") != path_id(logical, contract="lower"):
            raise WorkflowError(f"committed {key} custody has the wrong logical path")
        snapshot = _stable_file_snapshot(
            archived,
            max_bytes=shutdown_contract.MAX_LOG_BYTES,
        )
        if (
            list(snapshot["identity"]) != record.get("identity")
            or snapshot["length"] != record.get("length")
            or snapshot["last_write_ns"] != record.get("last_write_ns")
            or snapshot["sha256"] != record.get("sha256")
        ):
            raise WorkflowError(f"committed {key} descriptor custody changed")
        snapshots[key] = {"path": archived, "logical": logical, "snapshot": snapshot}

    prefix_payload = startup_replay.get("prefix_payload")
    prefix_hashes = startup_replay.get("prefix_record_hashes")
    if not isinstance(prefix_payload, bytes) or not isinstance(prefix_hashes, list):
        raise WorkflowError("archived startup replay omitted its prefix custody")
    pair = document.get("pair") if isinstance(document.get("pair"), Mapping) else {}
    lineage_manifest = load_json(
        snapshots["lineage_manifest"]["path"],
        "archived save lineage manifest",
    )
    try:
        lineage_chain = shutdown_contract._validate_lineage_chain(
            lineage_manifest,
            expected_run_id=roots.run_id,
            expected_mod_version=str(origin.get("mod_version") or ""),
            forward_step=str(pair.get("forward_step") or ""),
            inverse_step=str(pair.get("inverse_step") or ""),
        )
        initial = (
            lineage_manifest.get("initial")
            if isinstance(lineage_manifest.get("initial"), Mapping)
            else {}
        )
        activation_save = (
            initial.get("save") if isinstance(initial.get("save"), Mapping) else {}
        )
        activation_reload = (
            initial.get("reload") if isinstance(initial.get("reload"), Mapping) else {}
        )
        active_save_path_id = str(lineage_manifest.get("save_path_id") or "")
        origin_archive, _ = shutdown_contract._read_trusted_startup_session_archive(
            bridge_archive,
            origin,
            activation_save,
            certificate,
            prefix_payload,
            prefix_hashes,
        )
        activation_archive, _ = shutdown_contract._read_trusted_reload_session_prefix(
            bridge_archive,
            activation_reload,
            certificate,
            active_save_path_id,
            origin_archive["session_end_ts"],
        )
        final_source, termination, runtime_identity, _ = (
            shutdown_contract._read_trusted_session_log(
                snapshots["final_event_log"]["path"],
                bridge_archive,
                document["final_tip"],
                certificate,
                active_save_path_id,
            )
        )
    except shutdown_contract.ShutdownEvidenceError as exc:
        raise WorkflowError("archived shutdown runtime replay failed") from exc
    except Exception as exc:
        raise WorkflowError("archived shutdown runtime replay failed") from exc
    origin_archive = dict(origin_archive)
    origin_archive["path_id_lower"] = path_id(
        snapshots["origin_archive"]["logical"],
        contract="lower",
    )
    activation_archive = dict(activation_archive)
    activation_archive["path_id_lower"] = path_id(
        snapshots["activation_reload_archive"]["logical"],
        contract="lower",
    )
    final_source = dict(final_source)
    final_source["path"] = str(snapshots["final_event_log"]["logical"])
    final_tip = document.get("final_tip") if isinstance(document.get("final_tip"), Mapping) else {}
    replay_checks = {
        "origin archive": origin_archive == lineage.get("origin_archive"),
        "activation reload archive": activation_archive
        == lineage.get("activation_reload_archive"),
        "lineage origin": lineage_chain.get("origin") == lineage.get("origin"),
        "lineage activation": lineage_chain.get("activation") == lineage.get("activation"),
        "lineage checkpoints": lineage_chain.get("checkpoints") == lineage.get("checkpoints"),
        "lineage final": isinstance(lineage_chain.get("final"), Mapping)
        and lineage_chain["final"].get("session_id") == final_tip.get("session_id")
        and lineage_chain["final"].get("save_id_hash") == final_tip.get("save_id_hash")
        and lineage_chain["final"].get("snapshot_id") == final_tip.get("snapshot_id"),
        "final source": final_source == document.get("source"),
        "termination": termination == document.get("termination"),
        "game build": runtime_identity.get("game_build") == final_tip.get("game_build"),
        "mod version": runtime_identity.get("mod_version") == final_tip.get("mod_version"),
    }
    failed_replay_checks = [label for label, passed in replay_checks.items() if not passed]
    if failed_replay_checks:
        raise WorkflowError(
            "archived shutdown runtime replay conflicts with frozen projection: "
            + ", ".join(failed_replay_checks)
        )
    verify_tree(
        bridge_archive,
        runtime_binding.get("runtime_bridge_snapshot") or {},
        "committed archived runtime bridge",
    )
    for key, values in snapshots.items():
        final_snapshot = _stable_file_snapshot(
            values["path"],
            max_bytes=shutdown_contract.MAX_LOG_BYTES,
        )
        if final_snapshot != values["snapshot"]:
            raise WorkflowError(f"committed {key} changed across shutdown replay")
    _reject_shutdown_runtime_duplicate_custody(
        custody,
        logical_bridge,
        label="restored logical bridge",
    )


def capture_startup_consumption_preflight(
    args: argparse.Namespace,
    roots: Roots,
    bridge: pathlib.Path,
    lineage_path: pathlib.Path,
    shutdown_evidence: Mapping[str, Any],
    runtime_config: RuntimeConfigPreflight,
    old_archive: Optional[pathlib.Path],
) -> StartupConsumptionPreflight:
    bundle = normalized(args.startup_consumption_bundle)
    expected_bundle = normalized(lineage_path).parent / shutdown_contract.STARTUP_BUNDLE_DIRECTORY
    if not same_path(bundle, expected_bundle):
        raise WorkflowError("startup consumption bundle path is not the exact lineage-bound path")
    relative = bundle.relative_to(normalized(bridge)).as_posix()
    validate_relative_path(relative, "startup consumption bundle relative path")
    archived_bundle = roots.environment_dir / "rb" / pathlib.Path(relative)
    if os.path.lexists(archived_bundle):
        raise WorkflowError("startup consumption bundle has duplicate current/archived custody")
    if old_archive is not None and os.path.lexists(old_archive / pathlib.Path(relative)):
        raise WorkflowError("restored old bridge would create duplicate startup bundle custody")
    evidence_hash = require_hash(
        args.startup_consumption_evidence_hash,
        "startup consumption evidence external hash",
    )
    process_id = args.validation_process_id
    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
        raise WorkflowError("validation process id external anchor is invalid")
    custody = _startup_bundle_custody_snapshot(bundle, bridge)
    certificate = _startup_certificate_projection(shutdown_evidence)
    shutdown_document = shutdown_evidence.get("document")
    if not isinstance(shutdown_document, Mapping):
        raise WorkflowError("normal shutdown evidence document is missing")
    _shutdown_lineage_origin(shutdown_document, roots, certificate=certificate)
    certificate_hash = _cross_bind_startup_certificate(
        certificate,
        roots=roots,
        runtime_config=runtime_config,
        bundle_path=bundle,
        expected_evidence_hash=evidence_hash,
        expected_validation_process_id=process_id,
        custody=custody,
    )
    return StartupConsumptionPreflight(
        path=bundle,
        relative=relative,
        evidence_hash=evidence_hash,
        validation_process_id=process_id,
        certificate=certificate,
        certificate_hash=certificate_hash,
        custody=custody,
        custody_hash=sha256_bytes(canonical_bytes(custody)),
    )


def startup_consumption_binding(preflight: StartupConsumptionPreflight) -> dict[str, Any]:
    process = preflight.certificate["process"]
    session = preflight.certificate["session"]
    result = {
        "relative": preflight.relative,
        "evidence_hash": preflight.evidence_hash,
        "validation_process_id": preflight.validation_process_id,
        "certificate_hash": preflight.certificate_hash,
        "certificate": copy.deepcopy(preflight.certificate),
        "bundle_custody": copy.deepcopy(preflight.custody),
        "bundle_custody_hash": preflight.custody_hash,
        "session_id": session["session_id"],
        "validation_process_creation_token": process["creation_token"],
        "startup_archive_prefix_sha256": session["prefix_file_sha256"],
        "startup_archive_prefix_length": session["prefix_byte_length"],
    }
    if set(result) != STARTUP_CUSTODY_KEYS:
        raise WorkflowError("startup consumption custody binding field set is invalid")
    return result


def _verify_committed_startup_custody(
    roots: Roots,
    runtime_binding: Mapping[str, Any],
    *,
    runtime_config: RuntimeConfigPreflight,
    expected_bundle_path: pathlib.Path,
    expected_evidence_hash: str,
    expected_validation_process_id: int,
    expected_shutdown_evidence_hash: str,
) -> pathlib.Path:
    record = runtime_binding.get("startup_consumption")
    if not isinstance(record, Mapping) or set(record) != STARTUP_CUSTODY_KEYS:
        raise WorkflowError("committed runtime binding lost startup consumption custody")
    if (
        record.get("evidence_hash") != require_hash(
            expected_evidence_hash, "startup consumption evidence external hash"
        )
        or record.get("validation_process_id") != expected_validation_process_id
    ):
        raise WorkflowError("committed startup consumption custody conflicts with external anchors")
    relative_text = record.get("relative")
    if not isinstance(relative_text, str):
        raise WorkflowError("committed startup consumption relative path is invalid")
    relative = validate_relative_path(relative_text, "committed startup consumption path")
    current = roots.oni_root / BRIDGE_RELATIVE / pathlib.Path(*relative.parts)
    archived = roots.environment_dir / "rb" / pathlib.Path(*relative.parts)
    if not same_path(normalized(expected_bundle_path), current):
        raise WorkflowError("committed startup consumption logical path conflicts with CLI anchor")
    current_exists = os.path.lexists(current)
    archived_exists = os.path.lexists(archived)
    if current_exists or not archived_exists:
        raise WorkflowError("committed startup consumption custody is missing or duplicated")
    custody = record.get("bundle_custody")
    certificate = record.get("certificate")
    try:
        custody_hash = sha256_bytes(canonical_bytes(custody)) if isinstance(custody, Mapping) else None
        stored_certificate_hash = (
            contract_canonical_hash(certificate) if isinstance(certificate, Mapping) else None
        )
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("committed startup consumption custody is not canonically hashable") from exc
    if (
        not isinstance(custody, Mapping)
        or not isinstance(certificate, Mapping)
        or record.get("bundle_custody_hash") != custody_hash
        or record.get("certificate_hash") != stored_certificate_hash
    ):
        raise WorkflowError("committed startup consumption custody hashes are invalid")
    certificate_hash = _cross_bind_startup_certificate(
        certificate,
        roots=roots,
        runtime_config=runtime_config,
        bundle_path=current,
        expected_evidence_hash=str(record["evidence_hash"]),
        expected_validation_process_id=int(record["validation_process_id"]),
        custody=custody,
    )
    process = certificate["process"]
    session = certificate["session"]
    if (
        certificate_hash != record.get("certificate_hash")
        or record.get("session_id") != session.get("session_id")
        or record.get("validation_process_creation_token") != process.get("creation_token")
        or record.get("startup_archive_prefix_sha256") != session.get("prefix_file_sha256")
        or record.get("startup_archive_prefix_length") != session.get("prefix_byte_length")
    ):
        raise WorkflowError("committed startup consumption semantic projection is invalid")
    _verify_startup_bundle_custody(archived, roots.environment_dir / "rb", custody, "archived")

    frozen_shutdown = roots.environment_dir / "finalization" / "pair" / "shutdown.json"
    shutdown_record = runtime_binding.get("normal_shutdown_evidence")
    if (
        not isinstance(shutdown_record, Mapping)
        or set(shutdown_record) != NORMAL_SHUTDOWN_BINDING_KEYS
    ):
        raise WorkflowError("committed normal shutdown binding is missing")
    snapshot = _stable_file_snapshot(
        frozen_shutdown,
        max_bytes=MAX_JSON_BYTES,
        collect_bytes=True,
    )
    if snapshot["sha256"] != shutdown_record.get("file_sha256"):
        raise WorkflowError("frozen shutdown evidence lost its raw file custody")
    try:
        document = json.loads(
            bytes(snapshot["bytes"]).decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                WorkflowError(f"non-finite JSON constant: {item}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("frozen shutdown evidence is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise WorkflowError("frozen shutdown evidence root must be an object")
    expected_shutdown_hash = require_hash(
        expected_shutdown_evidence_hash,
        "normal shutdown evidence external hash",
    )
    if (
        set(document) != shutdown_contract.EVIDENCE_KEYS
        or document.get("schema") != shutdown_contract.EVIDENCE_SCHEMA
        or document.get("status") != "passed"
        or document.get("evidence_hash") != expected_shutdown_hash
        or document.get("evidence_hash") != shutdown_record.get("evidence_hash")
        or document.get("evidence_hash")
        != contract_hash_without(document, "evidence_hash", "frozen shutdown evidence")
    ):
        raise WorkflowError("frozen shutdown evidence lost its external/hash projection")
    lineage_origin = _shutdown_lineage_origin(document, roots, certificate=certificate)

    # A committed wrapper and all of its local hashes can be rewritten together.
    # Rebuild the startup projection from the archived immutable six-file bundle
    # using the externally authenticated v3 lineage origin rather than trusting
    # the projection copied into runtime-binding.json.
    try:
        replayed = shutdown_contract._validate_startup_bundle(
            bundle_dir=archived,
            expected_evidence_hash=str(record["evidence_hash"]),
            expected_process_id=int(record["validation_process_id"]),
            manifest_path=archived.parent / "manifest.json",
            bridge_dir=roots.environment_dir / "rb",
            expected_run_id=roots.run_id,
            lineage_origin=lineage_origin,
        )
    except shutdown_contract.ShutdownEvidenceError as exc:
        raise WorkflowError("archived startup consumption bundle replay failed") from exc
    except Exception as exc:
        raise WorkflowError("archived startup consumption bundle replay failed") from exc
    rebuilt = replayed.get("projection") if isinstance(replayed, Mapping) else None
    if not isinstance(rebuilt, Mapping):
        raise WorkflowError("archived startup consumption replay returned no projection")
    rebuilt_certificate = copy.deepcopy(dict(rebuilt))
    rebuilt_bundle = rebuilt_certificate.get("bundle")
    if not isinstance(rebuilt_bundle, dict):
        raise WorkflowError("archived startup consumption replay bundle projection is invalid")
    # The bundle's logical identity remains its pre-move Bridge path.  Only its
    # physical custody moved under environment/rb.
    rebuilt_bundle["path_id_lower"] = path_id(current, contract="lower")
    if (
        rebuilt_certificate != dict(certificate)
        or document.get("startup_consumption_certificate") != rebuilt_certificate
    ):
        raise WorkflowError("archived startup consumption replay conflicts with frozen certificate")
    _verify_committed_shutdown_runtime_custody(
        roots,
        runtime_binding,
        document,
        rebuilt_certificate,
        replayed,
    )
    # Close the verification interval with the same absence and descriptor
    # identities used at its beginning.  A duplicate logical bundle or a
    # same-name replacement that appears during replay must not be accepted.
    _verify_startup_bundle_custody(archived, roots.environment_dir / "rb", custody, "archived final")
    if os.path.lexists(current):
        raise WorkflowError("committed startup consumption custody became duplicated during verification")
    final_shutdown = _stable_file_snapshot(frozen_shutdown, max_bytes=MAX_JSON_BYTES)
    if (
        final_shutdown["identity"] != snapshot["identity"]
        or final_shutdown["length"] != snapshot["length"]
        or final_shutdown["last_write_ns"] != snapshot["last_write_ns"]
        or final_shutdown["sha256"] != snapshot["sha256"]
    ):
        raise WorkflowError("frozen shutdown evidence changed during committed verification")
    return archived


def build_runtime_binding(
    roots: Roots,
    manifest_hash: str,
    runtime_config: Mapping[str, Any],
    bootstrap_path: pathlib.Path,
    lineage_path: pathlib.Path,
    derivative_save: pathlib.Path,
    derivative_preview: pathlib.Path,
    loader_save: pathlib.Path,
    pair_report_path: pathlib.Path,
    pair_report: Mapping[str, Any],
    shutdown_evidence: Mapping[str, Any],
    shutdown_runtime_custody: Mapping[str, Any],
    startup_consumption: StartupConsumptionPreflight,
    bridge_snapshot: Mapping[str, Any],
    command_evidence: Sequence[Mapping[str, Any]],
    gate: Mapping[str, Any],
    pipeline_gate: Mapping[str, Any],
    counts: Mapping[str, int],
) -> dict[str, Any]:
    result = {
        "schema": RUNTIME_BINDING_SCHEMA,
        "run_id": roots.run_id,
        "environment_manifest_sha256": manifest_hash,
        "runtime_config": dict(runtime_config),
        "bootstrap": {
            "path_id": path_id(bootstrap_path, contract="lower"),
            "sha256": sha256_file(bootstrap_path),
        },
        "lineage": {
            "path_id": path_id(lineage_path, contract="lower"),
            "sha256": sha256_file(lineage_path),
            "derivative_save": {
                "path_id_lower": path_id(derivative_save, contract="lower"),
                "path_id_upper": path_id(derivative_save, contract="upper"),
                "state": file_state(derivative_save),
            },
            "derivative_preview": {
                "path_id_lower": path_id(derivative_preview, contract="lower"),
                "path_id_upper": path_id(derivative_preview, contract="upper"),
                "state": file_state(derivative_preview),
            },
        },
        "loader_before_restore": {
            "save": {
                "path_id_lower": path_id(loader_save, contract="lower"),
                "state": file_state(loader_save),
            },
            "preview": {
                "path_id_lower": path_id(loader_save.with_suffix(".png"), contract="lower"),
                "state": file_state(loader_save.with_suffix(".png")),
            },
        },
        "priority_pair_report": {
            "path_id_lower": path_id(pair_report_path, contract="lower"),
            "file_sha256": sha256_file(pair_report_path),
            "report_hash": pair_report["report_hash"],
        },
        "normal_shutdown_evidence": {
            "path_id_lower": path_id(shutdown_evidence["path"], contract="lower"),
            "file_sha256": shutdown_evidence["file_sha256"],
            "evidence_hash": shutdown_evidence["evidence_hash"],
            "source_path_id_lower": path_id(shutdown_evidence["source_path"], contract="lower"),
            "source_file_sha256": shutdown_evidence["source_file_sha256"],
        },
        "shutdown_runtime_custody": copy.deepcopy(dict(shutdown_runtime_custody)),
        "startup_consumption": startup_consumption_binding(startup_consumption),
        "runtime_bridge_snapshot": dict(bridge_snapshot),
        "commands": [
            {
                "id": item["id"],
                "step_id": item["step_id"],
                "command_intent_hash": item["command_intent_hash"],
                "final_command_hash": item["final_command_hash"],
                "approval_receipt_hash": item["approval_receipt_hash"],
                "source_policy_hash": item["source_policy_hash"],
                "source_frame_id": item["source_frame_id"],
                "source_frame_archive_file_sha256": item["source_frame_archive_file_sha256"],
                "done_command": {
                    "path_id_lower": path_id(item["command_path"], contract="lower"),
                    "file_sha256": item["command_file_sha256"],
                },
                "source_frame": {
                    "path_id_lower": path_id(item["source_frame_path"], contract="lower"),
                    "file_sha256": item["source_frame_archive_file_sha256"],
                    "currently_present": item["source_frame_currently_present"],
                },
                "confirmation_certificate": {
                    "path_id_lower": path_id(item["confirmation_certificate_path"], contract="lower"),
                    "file_sha256": item["confirmation_certificate_file_sha256"],
                    "outcomes_hash": item["confirmation_certificate_outcomes_hash"],
                    "currently_present": item["confirmation_certificate_currently_present"],
                },
                "automatic_approval_bundle": {
                    "path_id_lower": path_id(item["bundle_path"], contract="lower"),
                    "file_sha256": item["bundle_file_sha256"],
                    "bundle_hash": item["bundle_hash"],
                },
                "lineage_freeze": {
                    "path_id_lower": path_id(item["freeze_path"], contract="lower"),
                    "file_sha256": item["freeze_file_sha256"],
                    "freeze_hash": item["freeze_hash"],
                },
            }
            for item in command_evidence
        ],
        "finalize_gate": {
            "processes": dict(counts),
            "bridge": dict(gate),
            "command_pipeline": dict(pipeline_gate),
        },
    }
    if set(result) != RUNTIME_BINDING_KEYS:
        raise WorkflowError("runtime binding field set is invalid")
    return result


def verify_restored_state(
    roots: Roots,
    manifest: Mapping[str, Any],
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
    derivative_save: pathlib.Path,
    derivative_preview: pathlib.Path,
    runtime_binding: Mapping[str, Any],
    runtime_config: RuntimeConfigPreflight,
    final_root: pathlib.Path,
    startup_bundle_path: pathlib.Path,
    *,
    expected_startup_evidence_hash: str,
    expected_validation_process_id: int,
    expected_shutdown_evidence_hash: str,
) -> None:
    bindings = manifest["bindings"]
    if file_state(source_save) != bindings["source_save"]["state"]:
        raise WorkflowError("immutable source save changed during finalization")
    if file_state(source_save.with_suffix(".png")) != bindings["source_preview"]["state"]:
        raise WorkflowError("immutable source preview changed during finalization")
    if file_state(loader_save) != bindings["loader_save"]["state"]:
        raise WorkflowError("loader save content/timestamp was not restored exactly")
    if file_state(loader_save.with_suffix(".png")) != bindings["loader_preview"]["state"]:
        raise WorkflowError("loader preview content/timestamp was not restored exactly")
    if derivative_save.exists() or derivative_preview.exists():
        raise WorkflowError("lineage derivative pair remains under the ONI root")
    derivative_binding = runtime_binding["lineage"]
    if (
        file_state(final_root / "lineage-derivative" / derivative_save.name)
        != derivative_binding["derivative_save"]["state"]
        or file_state(final_root / "lineage-derivative" / derivative_preview.name)
        != derivative_binding["derivative_preview"]["state"]
    ):
        raise WorkflowError("recoverable lineage derivative evidence is not exact")
    loader_binding = runtime_binding["loader_before_restore"]
    if (
        file_state(final_root / "loader-before-restore" / loader_save.name)
        != loader_binding["save"]["state"]
        or file_state(final_root / "loader-before-restore" / loader_save.with_suffix(".png").name)
        != loader_binding["preview"]["state"]
    ):
        raise WorkflowError("recoverable pre-restore loader evidence is not exact")
    for record in manifest["backup"]["configs"]:
        actual = optional_file_state(roots.oni_root / pathlib.Path(record["relative"]))
        expected = {key: value for key, value in record.items() if key in {"exists", "state"}}
        if actual != expected:
            raise WorkflowError("config content/timestamp restoration is not exact")
    probe_record = manifest["backup"]["probe"]
    verify_tree(roots.oni_root / PROBE_RELATIVE, probe_record["snapshot"], "restored probe")
    old_record = manifest["backup"]["old_bridge"]
    verify_tree(roots.oni_root / BRIDGE_RELATIVE, old_record["snapshot"], "restored old bridge")
    archive_snapshot = manifest["backup"]["bridge_archive_root"]["snapshot"]
    verify_tree(
        roots.oni_root / BRIDGE_ARCHIVE_RELATIVE,
        archive_snapshot,
        "restored bridge archive root",
    )
    verify_tree(
        roots.environment_dir / "rb",
        runtime_binding["runtime_bridge_snapshot"],
        "preserved runtime bridge",
    )
    _verify_committed_startup_custody(
        roots,
        runtime_binding,
        runtime_config=runtime_config,
        expected_bundle_path=startup_bundle_path,
        expected_evidence_hash=expected_startup_evidence_hash,
        expected_validation_process_id=expected_validation_process_id,
        expected_shutdown_evidence_hash=expected_shutdown_evidence_hash,
    )


def _load_boundary_report_stable(
    path: pathlib.Path,
    label: str,
    *,
    boundary: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = normalized(path)
    assert_no_reparse(path, boundary)
    snapshot = _stable_file_snapshot(
        path,
        max_bytes=boundary_compare.MAX_REPORT_BYTES,
        collect_bytes=True,
    )
    raw = snapshot.get("bytes")
    if not isinstance(raw, bytes):
        raise PostValidationError(f"{label} boundary report has no stable payload")
    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                PostValidationError(f"non-finite boundary JSON constant: {item}")
            ),
        )
        if not isinstance(value, dict):
            raise PostValidationError(f"{label} boundary report root is not an object")
        boundary_compare.validate_report(value, label)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PostValidationError(f"{label} boundary report is invalid") from exc
    assert_no_reparse(path, boundary)
    return value, snapshot


def _compare_boundary_documents(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    allowed_loader_stem: str,
) -> dict[str, Any]:
    if before.get("phase") != "pre" or after.get("phase") != "post":
        raise PostValidationError("comparison requires pre and post phase reports")
    if before.get("run_id") != after.get("run_id"):
        raise PostValidationError("pre/post run_id mismatch")
    if before.get("oni_root_id") != after.get("oni_root_id"):
        raise PostValidationError("pre/post ONI root identity mismatch")
    try:
        allowed_loader_stem = boundary_compare.validate_allowed_stem(
            allowed_loader_stem
        )
    except (TypeError, ValueError, argparse.ArgumentTypeError) as exc:
        raise PostValidationError("allowed loader stem is invalid") from exc

    save_before = boundary_compare.entries(before, "saves", "pre")
    save_after = boundary_compare.entries(after, "saves", "post")
    save_diff = boundary_compare.compare_exact(save_before, save_after)
    after_save_index = boundary_compare.by_path_id(save_after)
    allowed_stem_id = boundary_compare.stem_id(allowed_loader_stem)
    unexpected_added_saves: list[dict[str, Any]] = []
    allowed_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for added_path_id in save_diff["added"]:
        item = after_save_index[added_path_id]
        item_stem_id = str(item["stem_id"])
        if item_stem_id != allowed_stem_id:
            unexpected_added_saves.append(
                {"path_id": added_path_id, "reason": "stem_not_allowed"}
            )
            continue
        key = (str(item["scope"]), str(item["parent_path_id"]), item_stem_id)
        allowed_groups[key].append(item)

    incomplete_allowed_pairs: list[dict[str, Any]] = []
    group_count_by_stem: dict[str, int] = defaultdict(int)
    for (scope, parent_path_id, item_stem_id), grouped in sorted(
        allowed_groups.items()
    ):
        group_count_by_stem[item_stem_id] += 1
        extensions = sorted(str(item["extension"]) for item in grouped)
        if extensions != [".png", ".sav"]:
            incomplete_allowed_pairs.append(
                {
                    "scope": scope,
                    "parent_path_id": parent_path_id,
                    "stem_id": item_stem_id,
                    "extensions": extensions,
                }
            )
    for item_stem_id, group_count in sorted(group_count_by_stem.items()):
        if group_count > 1:
            incomplete_allowed_pairs.append(
                {
                    "stem_id": item_stem_id,
                    "reason": "multiple_sibling_pairs_for_one_allowed_stem",
                    "group_count": group_count,
                }
            )

    config_diff = boundary_compare.compare_exact(
        boundary_compare.entries(before, "mod_configs", "pre"),
        boundary_compare.entries(after, "mod_configs", "post"),
    )
    probe_diff = boundary_compare.compare_exact(
        boundary_compare.entries(before, "installed_probe", "pre"),
        boundary_compare.entries(after, "installed_probe", "post"),
    )
    policy_diff = boundary_compare.compare_exact(
        boundary_compare.entries(before, "bridge_policies", "pre"),
        boundary_compare.entries(after, "bridge_policies", "post"),
    )
    queues = after["queues"]
    nonterminal = {
        stage: int(queues[stage])
        for stage in ("pending", "approving", "approved", "executing", "confirming")
    }
    processes = after["processes"]
    blockers: list[str] = []
    if save_diff["missing"]:
        blockers.append("preexisting_save_missing")
    if save_diff["changed"]:
        blockers.append("preexisting_save_changed")
    if unexpected_added_saves:
        blockers.append("unexpected_save_added")
    if incomplete_allowed_pairs:
        blockers.append("allowed_save_pair_incomplete_or_ambiguous")
    if config_diff["missing"] or config_diff["changed"] or config_diff["added"]:
        blockers.append("mod_or_player_config_not_restored")
    if probe_diff["missing"] or probe_diff["changed"] or probe_diff["added"]:
        blockers.append("installed_probe_not_restored")
    if policy_diff["missing"] or policy_diff["changed"] or policy_diff["added"]:
        blockers.append("bridge_policy_not_restored")
    if any(nonterminal.values()):
        blockers.append("nonterminal_command_queue_not_empty")
    if int(queues["temporary_files"]):
        blockers.append("command_temporary_files_present")
    if int(processes["oxygen_not_included_count"]):
        blockers.append("game_process_still_running")
    if int(processes["bridge_count"]):
        blockers.append("bridge_process_still_running")
    return {
        "schema": POST_BOUNDARY_COMPARISON_SCHEMA,
        "status": "passed" if not blockers else "blocked",
        "run_id": before["run_id"],
        "blockers": blockers,
        "preexisting_save_count": len(save_before),
        "new_save_count": len(save_diff["added"]),
        "unexpected_added_saves": unexpected_added_saves,
        "incomplete_allowed_pairs": incomplete_allowed_pairs,
        "save_diff": save_diff,
        "mod_config_diff": config_diff,
        "installed_probe_diff": probe_diff,
        "bridge_policy_diff": policy_diff,
        "nonterminal_queues": nonterminal,
        "temporary_file_count": int(queues["temporary_files"]),
        "processes": {
            "oxygen_not_included_count": int(processes["oxygen_not_included_count"]),
            "bridge_count": int(processes["bridge_count"]),
        },
    }


def _validate_capture_receipt(
    receipt: Mapping[str, Any],
    post: Mapping[str, Any],
    post_path: pathlib.Path,
    roots: Roots,
    post_snapshot: Mapping[str, Any],
) -> None:
    keys = {
        "schema",
        "status",
        "report_path_id",
        "report_relative_path",
        "report_sha256",
        "report_byte_length",
        "report_identity",
        "report_parent_identity",
        "save_count",
        "mod_config_count",
        "probe_file_count",
        "bridge_policy_count",
        "queue_nonterminal_count",
        "oxygen_not_included_count",
        "bridge_count",
    }
    nonnegative = {
        "save_count",
        "mod_config_count",
        "probe_file_count",
        "bridge_policy_count",
        "queue_nonterminal_count",
        "oxygen_not_included_count",
        "bridge_count",
    }
    queues = post["queues"]
    expected_relative = pathlib.PurePath(
        roots.run_id,
        post_path.name,
    ).as_posix()
    reported_relative = str(receipt.get("report_relative_path") or "").replace("\\", "/")
    report_identity = receipt.get("report_identity")
    parent_identity = receipt.get("report_parent_identity")
    try:
        parent_metadata = os.lstat(post_path.parent)
    except OSError as exc:
        raise PostValidationError("post-boundary report parent is unavailable") from exc
    identity_keys = {"volume_serial_number", "file_index", "number_of_links"}
    parent_identity_keys = {"volume_serial_number", "file_index"}
    identity_integers = (
        list(report_identity.values())
        if isinstance(report_identity, Mapping)
        else []
    ) + (
        list(parent_identity.values())
        if isinstance(parent_identity, Mapping)
        else []
    )
    snapshot_identity = post_snapshot.get("identity")
    if (
        set(receipt) != keys
        or receipt.get("schema") != "oni.live-safety-boundary-capture-receipt.v2"
        or receipt.get("status") != "captured"
        or receipt.get("report_path_id") != path_id(post_path, contract="lower")
        or reported_relative != expected_relative
        or receipt.get("report_sha256") != post_snapshot.get("sha256")
        or receipt.get("report_byte_length") != post_snapshot.get("length")
        or not isinstance(report_identity, Mapping)
        or set(report_identity) != identity_keys
        or not isinstance(parent_identity, Mapping)
        or set(parent_identity) != parent_identity_keys
        or len(identity_integers) != 5
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in identity_integers
        )
        or report_identity.get("number_of_links") != 1
        or not isinstance(snapshot_identity, (list, tuple))
        or len(snapshot_identity) != 3
        or report_identity.get("file_index") != int(snapshot_identity[1])
        or parent_identity.get("file_index") != int(parent_metadata.st_ino)
        or report_identity.get("volume_serial_number")
        != parent_identity.get("volume_serial_number")
        or report_identity.get("file_index") == parent_identity.get("file_index")
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or bool(getattr(parent_metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)
        or any(
            isinstance(receipt.get(key), bool)
            or not isinstance(receipt.get(key), int)
            or int(receipt[key]) < 0
            for key in nonnegative
        )
        or receipt.get("save_count") != post["saves"]["count"]
        or receipt.get("mod_config_count") != post["mod_configs"]["count"]
        or receipt.get("probe_file_count") != post["installed_probe"]["count"]
        or receipt.get("bridge_policy_count") != post["bridge_policies"]["count"]
        or receipt.get("queue_nonterminal_count")
        != sum(
            int(queues[stage])
            for stage in ("pending", "approving", "approved", "executing", "confirming")
        )
        or receipt.get("oxygen_not_included_count")
        != post["processes"]["oxygen_not_included_count"]
        or receipt.get("bridge_count") != post["processes"]["bridge_count"]
    ):
        raise PostValidationError("post-boundary capture receipt/report binding is invalid")


def _verify_pre_boundary_report_anchor(
    path: pathlib.Path,
    document: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    roots: Roots,
    manifest: Mapping[str, Any],
) -> None:
    """Bind every descriptor replay of the pre report to the prepare manifest."""

    bindings = manifest.get("bindings")
    pre_binding = bindings.get("pre_boundary") if isinstance(bindings, Mapping) else None
    if (
        not isinstance(pre_binding, Mapping)
        or set(pre_binding) != {"path_id", "sha256", "oni_root_id"}
        or pre_binding.get("path_id") != path_id(path, contract="lower")
        or pre_binding.get("sha256") != snapshot.get("sha256")
        or pre_binding.get("oni_root_id") != document.get("oni_root_id")
        or document.get("oni_root_id") != path_id(roots.oni_root, contract="lower")
    ):
        raise WorkflowError("pre-boundary report lost its immutable manifest anchor")


def _post_boundary_semantic_projection(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic acceptance semantics, excluding attempt provenance."""

    provenance = {
        "pre_report_sha256",
        "post_report_sha256",
        "pre_report_relative",
        "post_report_relative",
        "pre_report_path_id_lower",
        "post_report_path_id_lower",
        "capture_receipt",
        "capture_receipt_hash",
        "comparison_engine",
    }
    if set(report) != POST_BOUNDARY_REPORT_KEYS:
        raise WorkflowError("post-boundary comparison shape is invalid")
    return {
        key: copy.deepcopy(value)
        for key, value in report.items()
        if key not in provenance
    }


def run_post_boundary(
    args: argparse.Namespace,
    roots: Roots,
    final_root: pathlib.Path,
    manifest: Mapping[str, Any],
    *,
    boundary_report_root: Optional[pathlib.Path] = None,
    comparison_path: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    report_root = normalized(boundary_report_root or roots.boundary_report_root)
    comparison_target = normalized(
        comparison_path or (final_root / "post-boundary-comparison.json")
    )
    if (
        not path_within(report_root, roots.evidence_root)
        or paths_overlap(report_root, roots.oni_root)
        or not path_within(comparison_target, roots.evidence_root)
        or paths_overlap(comparison_target, roots.oni_root)
    ):
        raise PostValidationError("post-boundary evidence escaped its evidence-only boundary")
    if report_root.exists() or os.path.lexists(report_root):
        assert_no_reparse(report_root, roots.evidence_root)
    else:
        assert_no_reparse(report_root, roots.evidence_root, allow_missing_leaf=True)
    assert_no_reparse(comparison_target.parent, roots.evidence_root)
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise PostValidationError("PowerShell is unavailable for post-boundary capture")
    capture = [
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(CAPTURE_SCRIPT),
        "-RunId",
        roots.run_id,
        "-Phase",
        "post",
        "-OniUserRoot",
        str(roots.oni_root),
        "-ReportRoot",
        str(report_root),
    ]
    if roots.test_root is not None:
        capture.extend(
            [
                "-TestRoot",
                str(roots.test_root),
                "-ProcessStateFixture",
                str(normalized(args.process_state_fixture)),
            ]
        )
    completed = subprocess.run(
        capture,
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise PostValidationError("post-boundary capture failed")
    try:
        receipt = json.loads(
            completed.stdout.lstrip("\ufeff"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                PostValidationError(f"non-finite capture receipt constant: {item}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PostValidationError("post-boundary capture returned invalid receipt") from exc
    if not isinstance(receipt, dict):
        raise PostValidationError("post-boundary capture receipt root is invalid")

    pre = normalized(args.pre_boundary)
    post = report_root / roots.run_id / "post-boundary.json"
    if not path_within(pre, roots.boundary_report_root):
        raise PostValidationError("pre-boundary input escaped its report root")
    pre_document, pre_snapshot = _load_boundary_report_stable(
        pre,
        "pre",
        boundary=roots.evidence_root,
    )
    _verify_pre_boundary_report_anchor(
        pre,
        pre_document,
        pre_snapshot,
        roots,
        manifest,
    )
    post_document, post_snapshot = _load_boundary_report_stable(
        post,
        "post",
        boundary=roots.evidence_root,
    )
    _validate_capture_receipt(
        receipt,
        post_document,
        post,
        roots,
        post_snapshot,
    )
    report = _compare_boundary_documents(
        pre_document,
        post_document,
        allowed_loader_stem=args.loader_stem,
    )
    report.update(
        {
            "pre_report_sha256": pre_snapshot["sha256"],
            "post_report_sha256": post_snapshot["sha256"],
            "pre_report_relative": pre.relative_to(roots.evidence_root).as_posix(),
            "post_report_relative": post.relative_to(roots.evidence_root).as_posix(),
            "pre_report_path_id_lower": path_id(pre, contract="lower"),
            "post_report_path_id_lower": path_id(post, contract="lower"),
            "capture_receipt": receipt,
            "capture_receipt_hash": sha256_bytes(canonical_bytes(receipt)),
            "comparison_engine": "manager_descriptor_stable_in_process_v1",
            "allowed_loader_stem": args.loader_stem,
        }
    )
    for path, original, label in (
        (pre, pre_snapshot, "pre"),
        (post, post_snapshot, "post"),
    ):
        current = _stable_file_snapshot(
            path,
            max_bytes=boundary_compare.MAX_REPORT_BYTES,
        )
        if any(
            current[key] != original[key]
            for key in ("identity", "length", "last_write_ns", "sha256")
        ):
            raise PostValidationError(
                f"{label} boundary report changed during in-process comparison"
            )
    write_json_once_by_rename(comparison_target, report, "post-boundary comparison")
    expected_raw = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if sha256_file(comparison_target) != sha256_bytes(expected_raw):
        raise PostValidationError("post-boundary comparison publication changed")
    if report.get("status") != "passed":
        raise PostValidationError("post-boundary comparison blocked final acceptance")
    reopened, reopened_sha256 = _validated_post_boundary_report(
        roots,
        final_root,
        manifest,
        report_path=comparison_target,
    )
    if reopened != report or reopened_sha256 != sha256_bytes(expected_raw):
        raise PostValidationError("post-boundary comparison publication changed")
    if report.get("unexpected_added_saves") or report.get("incomplete_allowed_pairs"):
        raise PostValidationError("post-boundary comparison admitted an unexpected save")
    return report


def _seal_finalize_document(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    if "document_hash" in result:
        raise WorkflowError("finalize document hash field was already populated")
    result["document_hash"] = contract_canonical_hash(result)
    return result


def _validate_finalize_document_hash(
    document: Mapping[str, Any],
    expected_keys: frozenset[str],
    label: str,
) -> None:
    if (
        set(document) != expected_keys
        or document.get("document_hash")
        != contract_hash_without(document, "document_hash", label)
    ):
        raise WorkflowError(f"{label} shape or self-hash is invalid")


def _verify_finalize_terminal_journal(
    roots: Roots,
    result_path: pathlib.Path,
    result: Mapping[str, Any],
) -> bool:
    """Verify the unique terminal acknowledgement for one immutable result.

    A missing acknowledgement is distinguishable from a conflicting one so a
    recovery candidate may converge the narrow post-publication crash gap. A
    present terminal record must be the journal tail and bind the raw result
    bytes plus every shutdown/startup custody value it repeats.
    """

    entries = verify_journal(roots.environment_dir / "restore-journal.jsonl")
    terminal = [
        entry
        for entry in entries
        if entry.get("event") in {"environment_finalized", "environment_finalized_recovered"}
    ]
    if not terminal:
        return False
    if len(terminal) != 1 or terminal[0] is not entries[-1]:
        raise WorkflowError("finalize terminal journal acknowledgement is duplicated or not terminal")
    entry = terminal[0]
    data = entry.get("data")
    if not isinstance(data, Mapping):
        raise WorkflowError("finalize terminal journal data is invalid")
    common_keys = {
        "finalize_result_sha256",
        "normal_shutdown_evidence_hash",
        "startup_consumption_evidence_hash",
        "startup_session_id",
        "validation_process_creation_token",
        "startup_archive_prefix_sha256",
        "startup_archive_prefix_length",
    }
    event = entry.get("event")
    expected_keys = common_keys | ({"post_boundary_status"} if event == "environment_finalized" else {"state"})
    if set(data) != expected_keys:
        raise WorkflowError("finalize terminal journal data shape is invalid")
    if (
        data.get("finalize_result_sha256") != sha256_file(result_path)
        or data.get("normal_shutdown_evidence_hash")
        != result.get("normal_shutdown_evidence_hash")
        or data.get("startup_consumption_evidence_hash")
        != result.get("startup_consumption_evidence_hash")
        or data.get("startup_session_id") != result.get("startup_session_id")
        or data.get("validation_process_creation_token")
        != result.get("validation_process_creation_token")
        or data.get("startup_archive_prefix_sha256")
        != result.get("startup_archive_prefix_sha256")
        or data.get("startup_archive_prefix_length")
        != result.get("startup_archive_prefix_length")
        or (
            event == "environment_finalized"
            and data.get("post_boundary_status") != result.get("post_boundary_status")
        )
        or (
            event == "environment_finalized_recovered"
            and not isinstance(data.get("state"), str)
        )
    ):
        raise WorkflowError("finalize terminal journal does not bind the immutable result")
    return True


def _validated_post_boundary_report(
    roots: Roots,
    final_root: pathlib.Path,
    manifest: Mapping[str, Any],
    *,
    report_path: Optional[pathlib.Path] = None,
) -> tuple[dict[str, Any], str]:
    path = normalized(report_path or (final_root / "post-boundary-comparison.json"))
    if not path_within(path, roots.evidence_root) or paths_overlap(path, roots.oni_root):
        raise WorkflowError("post-boundary comparison escaped its evidence-only boundary")
    assert_no_reparse(path, roots.evidence_root)
    snapshot = _stable_file_snapshot(path, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
    try:
        report = json.loads(
            bytes(snapshot["bytes"]).decode("utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                WorkflowError(f"non-finite JSON constant: {item}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("post-boundary comparison is not valid UTF-8 JSON") from exc
    if not isinstance(report, dict) or set(report) != POST_BOUNDARY_REPORT_KEYS:
        raise WorkflowError("post-boundary comparison shape is invalid")
    try:
        pre_relative = validate_relative_path(
            str(report.get("pre_report_relative") or ""),
            "pre-boundary report relative path",
        )
        post_relative = validate_relative_path(
            str(report.get("post_report_relative") or ""),
            "post-boundary report relative path",
        )
    except WorkflowError:
        raise
    pre_path = roots.evidence_root / pathlib.Path(*pre_relative.parts)
    post_path = roots.evidence_root / pathlib.Path(*post_relative.parts)
    pre_document, pre_snapshot = _load_boundary_report_stable(
        pre_path,
        "bound pre",
        boundary=roots.evidence_root,
    )
    _verify_pre_boundary_report_anchor(
        pre_path,
        pre_document,
        pre_snapshot,
        roots,
        manifest,
    )
    post_document, post_snapshot = _load_boundary_report_stable(
        post_path,
        "bound post",
        boundary=roots.evidence_root,
    )
    receipt = report.get("capture_receipt")
    if not isinstance(receipt, Mapping):
        raise WorkflowError("post-boundary comparison capture receipt is invalid")
    try:
        _validate_capture_receipt(
            receipt,
            post_document,
            post_path,
            roots,
            post_snapshot,
        )
        reconstructed = _compare_boundary_documents(
            pre_document,
            post_document,
            allowed_loader_stem=str(report.get("allowed_loader_stem") or ""),
        )
    except PostValidationError as exc:
        raise WorkflowError("post-boundary raw input replay failed") from exc
    reconstructed.update(
        {
            "pre_report_sha256": pre_snapshot["sha256"],
            "post_report_sha256": post_snapshot["sha256"],
            "pre_report_relative": pre_relative.as_posix(),
            "post_report_relative": post_relative.as_posix(),
            "pre_report_path_id_lower": path_id(pre_path, contract="lower"),
            "post_report_path_id_lower": path_id(post_path, contract="lower"),
            "capture_receipt": dict(receipt),
            "capture_receipt_hash": sha256_bytes(canonical_bytes(receipt)),
            "comparison_engine": "manager_descriptor_stable_in_process_v1",
            "allowed_loader_stem": str(report.get("allowed_loader_stem") or ""),
        }
    )
    if report != reconstructed:
        raise WorkflowError(
            "post-boundary comparison does not exactly replay from its raw inputs"
        )
    for raw_path, original, label in (
        (pre_path, pre_snapshot, "pre"),
        (post_path, post_snapshot, "post"),
    ):
        current = _stable_file_snapshot(
            raw_path,
            max_bytes=boundary_compare.MAX_REPORT_BYTES,
        )
        if any(
            current[key] != original[key]
            for key in ("identity", "length", "last_write_ns", "sha256")
        ):
            raise WorkflowError(
                f"{label} boundary report changed during raw replay validation"
            )
    if (
        report.get("schema") != POST_BOUNDARY_COMPARISON_SCHEMA
        or report.get("status") != "passed"
        or report.get("run_id") != roots.run_id
        or report.get("blockers") != []
        or report.get("unexpected_added_saves") != []
        or report.get("incomplete_allowed_pairs") != []
        or report.get("temporary_file_count") != 0
        or report.get("processes")
        != {"oxygen_not_included_count": 0, "bridge_count": 0}
        or any(int(value) != 0 for value in (report.get("nonterminal_queues") or {}).values())
    ):
        raise WorkflowError("post-boundary comparison is not an exact passing report")
    final_snapshot = _stable_file_snapshot(path, max_bytes=MAX_JSON_BYTES)
    if (
        final_snapshot["identity"] != snapshot["identity"]
        or final_snapshot["length"] != snapshot["length"]
        or final_snapshot["last_write_ns"] != snapshot["last_write_ns"]
        or final_snapshot["sha256"] != snapshot["sha256"]
    ):
        raise WorkflowError("post-boundary comparison changed during descriptor validation")
    return report, str(snapshot["sha256"])


def _expected_finalize_result(
    roots: Roots,
    final_root: pathlib.Path,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    runtime_binding: Mapping[str, Any],
    runtime_binding_sha256: str,
    runtime_config: RuntimeConfigPreflight,
    *,
    post_boundary_report_path: Optional[pathlib.Path] = None,
    expected_post_boundary_report: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    post_boundary_report, post_boundary_sha256 = _validated_post_boundary_report(
        roots,
        final_root,
        manifest,
        report_path=post_boundary_report_path,
    )
    if expected_post_boundary_report is not None:
        expected_raw = json.dumps(
            expected_post_boundary_report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        if (
            post_boundary_report != dict(expected_post_boundary_report)
            or post_boundary_sha256 != sha256_bytes(expected_raw)
        ):
            raise WorkflowError(
                "post-boundary comparison changed after its in-process authorization"
            )
    startup = runtime_binding.get("startup_consumption")
    shutdown = runtime_binding.get("normal_shutdown_evidence")
    commands = runtime_binding.get("commands")
    backup = manifest.get("backup") if isinstance(manifest.get("backup"), Mapping) else {}
    configs = backup.get("configs") if isinstance(backup.get("configs"), list) else None
    old_bridge = backup.get("old_bridge") if isinstance(backup.get("old_bridge"), Mapping) else {}
    old_bridge_snapshot = (
        old_bridge.get("snapshot") if isinstance(old_bridge.get("snapshot"), Mapping) else {}
    )
    if (
        not isinstance(startup, Mapping)
        or set(startup) != STARTUP_CUSTODY_KEYS
        or not isinstance(shutdown, Mapping)
        or set(shutdown) != NORMAL_SHUTDOWN_BINDING_KEYS
        or not isinstance(commands, list)
        or configs is None
        or not isinstance(old_bridge_snapshot.get("exists"), bool)
    ):
        raise WorkflowError("committed runtime/manifest bindings cannot reconstruct finalize result")
    return _seal_finalize_document(
        {
            "schema": FINALIZE_RESULT_SCHEMA,
            "status": "finalized",
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "runtime_binding_sha256": runtime_binding_sha256,
            "runtime_config": dict(runtime_config.binding),
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "expected_release_dll_sha256": runtime_config.binding["release_dll"][
                "external_expected_sha256"
            ],
            "command_count": len(commands),
            "normal_shutdown_evidence_hash": shutdown["evidence_hash"],
            "startup_consumption_evidence_hash": startup["evidence_hash"],
            "startup_certificate_projection_hash": startup["certificate_hash"],
            "startup_bundle_custody_hash": startup["bundle_custody_hash"],
            "validation_process_id": startup["validation_process_id"],
            "startup_session_id": startup["session_id"],
            "validation_process_creation_token": startup[
                "validation_process_creation_token"
            ],
            "startup_archive_prefix_sha256": startup[
                "startup_archive_prefix_sha256"
            ],
            "startup_archive_prefix_length": startup[
                "startup_archive_prefix_length"
            ],
            "derivative_pair_archived": True,
            "loader_pair_restored": True,
            "config_count": len(configs),
            "probe_tree_restored": True,
            "old_bridge_restored": bool(old_bridge_snapshot["exists"]),
            "post_boundary_status": "passed",
            "post_boundary_report_sha256": post_boundary_sha256,
            "allowed_new_save_stem_count": 1,
        }
    )


def publish_finalize_recovery(
    roots: Roots,
    manifest_hash: str,
    state: str,
    *,
    runtime_config: RuntimeConfigPreflight,
    runtime_binding_sha256: str,
    startup_consumption: Mapping[str, Any],
    normal_shutdown_evidence_hash: str,
    candidate_result: Optional[Mapping[str, Any]] = None,
    error_type: str,
) -> pathlib.Path:
    recovery_path = roots.environment_dir / "finalization" / "finalize-recovery.json"
    candidate = dict(candidate_result) if candidate_result is not None else None
    payload = _seal_finalize_document({
        "schema": FINALIZE_RECOVERY_SCHEMA,
        "status": "outcome_unknown",
        "run_id": roots.run_id,
        "environment_manifest_sha256": manifest_hash,
        "state": state,
        "physical_commit_non_replay": True,
        "recovery_required": True,
        "error_type": error_type,
        "runtime_binding_sha256": runtime_binding_sha256,
        "runtime_config": dict(runtime_config.binding),
        "runtime_config_binding_hash": runtime_config.binding_hash,
        "runtime_config_evidence_sha256": runtime_config.file_sha256,
        "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
        "expected_release_dll_sha256": runtime_config.binding["release_dll"][
            "external_expected_sha256"
        ],
        "normal_shutdown_evidence_hash": normal_shutdown_evidence_hash,
        "startup_consumption_evidence_hash": startup_consumption["evidence_hash"],
        "startup_certificate_projection_hash": startup_consumption["certificate_hash"],
        "startup_bundle_custody_hash": startup_consumption["bundle_custody_hash"],
        "validation_process_id": startup_consumption["validation_process_id"],
        "startup_session_id": startup_consumption["session_id"],
        "validation_process_creation_token": startup_consumption[
            "validation_process_creation_token"
        ],
        "startup_archive_prefix_sha256": startup_consumption[
            "startup_archive_prefix_sha256"
        ],
        "startup_archive_prefix_length": startup_consumption[
            "startup_archive_prefix_length"
        ],
        "candidate_result": candidate,
        "candidate_result_hash": (
            contract_canonical_hash(candidate) if candidate is not None else None
        ),
    })
    if recovery_path.exists():
        if load_json(recovery_path, "finalize recovery") != payload:
            raise WorkflowError("finalize recovery evidence conflicts with the committed outcome")
    else:
        write_json_once_by_rename(recovery_path, payload, "finalize recovery")
    return recovery_path


def append_post_boundary_recovery_latch(
    roots: Roots,
    recovery_path: pathlib.Path,
) -> dict[str, Any]:
    """Record the observed failure phase for audit; never use it as authority."""

    recovery = load_json(recovery_path, "finalize recovery")
    state = "post_boundary_or_publication_blocked_after_physical_commit"
    if (
        recovery.get("state") != state
        or recovery.get("candidate_result") is not None
        or recovery.get("candidate_result_hash") is not None
    ):
        raise WorkflowError("post-boundary recovery latch source is invalid")
    return append_journal(
        roots.environment_dir / "restore-journal.jsonl",
        "post_boundary_blocked",
        {
            "environment_restored": True,
            "recovery_state": state,
            "finalize_recovery_sha256": sha256_file(recovery_path),
        },
    )


def _new_finalize_revalidation_attempt(
    roots: Roots,
    final_root: pathlib.Path,
) -> tuple[
    pathlib.Path,
    pathlib.Path,
    pathlib.Path,
    tuple[int, int, int],
    tuple[int, int, int],
    tuple[int, int, int],
]:
    """Create one short, owned evidence directory for a fresh recovery check."""

    final_root = normalized(final_root)
    assert_no_reparse(final_root, roots.evidence_root)
    final_root_identity = _owned_node_identity(os.lstat(final_root))
    attempt_id = uuid.uuid4().hex[:12]
    base = final_root / "rv"
    attempt = base / attempt_id
    report_root = attempt / "b"
    comparison_path = attempt / "c.json"
    prospective_paths = (
        attempt,
        report_root / roots.run_id / "post-boundary.json",
        report_root / roots.run_id / ".post-ffffffffffffffffffffffffffffffff.tmp",
        comparison_path,
        comparison_path.parent / ".j-ffffffffffffffff.tmp",
        final_root / "revalidation-comparison.json",
        final_root / "revalidation-passed.json",
    )
    if any(
        len(str(normalized(path)).encode("utf-16-le")) // 2 + 1
        > MAX_LEGACY_PATH_UTF16_UNITS
        for path in prospective_paths
    ):
        raise WorkflowError(
            "finalize revalidation evidence exceeds the legacy Win32 path budget"
        )
    base_existed = os.path.lexists(base)
    if base_existed:
        if not base.is_dir() or is_reparse(base):
            raise WorkflowError("finalize revalidation root is unsafe")
    else:
        try:
            base.mkdir()
        except FileExistsError as exc:
            raise WorkflowError(
                "finalize revalidation root appeared concurrently"
            ) from exc
    assert_no_reparse(base, roots.evidence_root)
    base_identity = _owned_node_identity(os.lstat(base))
    try:
        attempt.mkdir()
    except FileExistsError as exc:
        raise WorkflowError("finalize revalidation attempt id collided") from exc
    attempt_identity = _owned_node_identity(os.lstat(attempt))
    if (
        not _path_matches_owned_identity(
            final_root,
            final_root_identity,
            kind="directory",
        )
        or
        not _path_matches_owned_identity(base, base_identity, kind="directory")
        or not _path_matches_owned_identity(attempt, attempt_identity, kind="directory")
    ):
        raise WorkflowError("finalize revalidation directory ownership changed")
    return (
        attempt,
        report_root,
        comparison_path,
        final_root_identity,
        base_identity,
        attempt_identity,
    )


def _verify_finalize_revalidation_directories(
    final_root: pathlib.Path,
    attempt: pathlib.Path,
    final_root_identity: tuple[int, int, int],
    base_identity: tuple[int, int, int],
    attempt_identity: tuple[int, int, int],
) -> None:
    if (
        not _path_matches_owned_identity(
            final_root,
            final_root_identity,
            kind="directory",
        )
        or not _path_matches_owned_identity(final_root / "rv", base_identity, kind="directory")
        or not _path_matches_owned_identity(attempt, attempt_identity, kind="directory")
    ):
        raise WorkflowError("finalize revalidation evidence directory was name-swapped")


def _validate_finalize_revalidation_transition(
    path: pathlib.Path,
    *,
    roots: Roots,
    manifest_hash: str,
    runtime_binding_sha256: str,
    source_recovery_sha256: Optional[str],
    source_recovery_state: str,
    report_sha256: str,
    candidate_result: Mapping[str, Any],
) -> dict[str, Any]:
    transition = load_json(path, "finalize revalidation transition")
    _validate_finalize_document_hash(
        transition,
        FINALIZE_REVALIDATION_KEYS,
        "finalize revalidation transition",
    )
    expected = _seal_finalize_document(
        {
            "schema": FINALIZE_REVALIDATION_SCHEMA,
            "status": "passed",
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "runtime_binding_sha256": runtime_binding_sha256,
            "source_recovery_sha256": source_recovery_sha256,
            "source_recovery_state": source_recovery_state,
            "post_boundary_report_sha256": report_sha256,
            "candidate_result": dict(candidate_result),
            "candidate_result_hash": contract_canonical_hash(candidate_result),
        }
    )
    expected_raw = json.dumps(
        expected,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if transition != expected or sha256_file(path) != sha256_bytes(expected_raw):
        raise WorkflowError("finalize revalidation transition is not exact")
    return transition


def _verify_current_save_set_against_pre_boundary(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
) -> None:
    pre_path = normalized(args.pre_boundary)
    pre, pre_snapshot = _load_boundary_report_stable(
        pre_path,
        "committed pre",
        boundary=roots.evidence_root,
    )
    _verify_pre_boundary_report_anchor(
        pre_path,
        pre,
        pre_snapshot,
        roots,
        manifest,
    )
    def boundary_timestamp_ns(value: Any) -> int:
        if not isinstance(value, str):
            raise WorkflowError("boundary save last_write_utc is invalid")
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})",
            value,
        )
        if match is None:
            raise WorkflowError("boundary save last_write_utc is invalid")
        offset = "+00:00" if match.group(3) == "Z" else match.group(3)
        try:
            parsed = dt.datetime.fromisoformat(match.group(1) + offset)
        except ValueError as exc:
            raise WorkflowError("boundary save last_write_utc is invalid") from exc
        if parsed.tzinfo is None:
            raise WorkflowError("boundary save last_write_utc lacks an offset")
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        delta = parsed.astimezone(dt.timezone.utc) - epoch
        whole_seconds = delta.days * 86_400 + delta.seconds
        fractional_ns = int((match.group(2) or "").ljust(9, "0"))
        return whole_seconds * 1_000_000_000 + fractional_ns

    expected: dict[str, tuple[int, str, int]] = {
        str(item["path_id"]): (
            int(item["length"]),
            str(item["sha256"]),
            boundary_timestamp_ns(item.get("last_write_utc")),
        )
        for item in boundary_compare.entries(pre, "saves", "committed pre")
    }
    bindings = manifest.get("bindings")
    if not isinstance(bindings, Mapping):
        raise WorkflowError("environment manifest bindings are unavailable")
    loader_save = normalized(args.loader_save)
    loader_preview = loader_save.with_suffix(".png")
    source_state = bindings.get("source_save")
    preview_state = bindings.get("source_preview")
    if not isinstance(source_state, Mapping) or not isinstance(preview_state, Mapping):
        raise WorkflowError("environment manifest source pair binding is invalid")
    for path, record in (
        (loader_save, source_state),
        (loader_preview, preview_state),
    ):
        state = record.get("state")
        if not isinstance(state, Mapping):
            raise WorkflowError("environment manifest loader restoration state is invalid")
        expected[path_id(path, contract="lower")] = (
            int(state["length"]),
            str(state["sha256"]),
            int(state["last_write_ns"]),
        )

    actual: dict[str, tuple[int, str, int]] = {}
    for relative in ("save_files", "cloud_save_files"):
        save_root = roots.oni_root / relative
        if not save_root.exists():
            continue
        assert_no_reparse(save_root, roots.oni_root)
        for path in sorted(save_root.rglob("*"), key=lambda item: str(item).casefold()):
            if is_reparse(path):
                raise WorkflowError("committed save tree contains a reparse point")
            if not path.is_file() or path.suffix.lower() not in {".sav", ".png"}:
                continue
            snapshot = _stable_file_snapshot(path)
            identifier = path_id(path, contract="lower")
            if identifier in actual:
                raise WorkflowError("committed save tree produced a duplicate path id")
            actual[identifier] = (
                int(snapshot["length"]),
                str(snapshot["sha256"]),
                int(snapshot["last_write_ns"]),
            )
    if actual != expected:
        raise WorkflowError(
            "current save set differs from pre-boundary plus the exact restored loader pair"
        )
    final_pre = _stable_file_snapshot(
        pre_path,
        max_bytes=boundary_compare.MAX_REPORT_BYTES,
    )
    if any(
        final_pre[key] != pre_snapshot[key]
        for key in ("identity", "length", "last_write_ns", "sha256")
    ):
        raise WorkflowError("pre-boundary report changed during committed save-set validation")


def _verify_committed_restored_state(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    runtime_binding: Mapping[str, Any],
    runtime_config: RuntimeConfigPreflight,
    final_root: pathlib.Path,
) -> None:
    source_save = normalized(args.source_save)
    loader_save = normalized(args.loader_save)
    derivative_archive = final_root / "lineage-derivative"
    assert_no_reparse(derivative_archive, roots.evidence_root)
    archived = [
        path
        for path in derivative_archive.iterdir()
        if path.is_file() and not is_reparse(path)
    ]
    derivative_saves = [path for path in archived if path.suffix.lower() == ".sav"]
    derivative_previews = [path for path in archived if path.suffix.lower() == ".png"]
    if (
        len(archived) != 2
        or len(derivative_saves) != 1
        or len(derivative_previews) != 1
        or derivative_saves[0].stem != derivative_previews[0].stem
    ):
        raise WorkflowError("committed derivative archive is not one exact save/preview pair")
    derivative_save = loader_save.parent / derivative_saves[0].name
    derivative_preview = loader_save.parent / derivative_previews[0].name
    lineage = runtime_binding.get("lineage")
    derivative_save_binding = (
        lineage.get("derivative_save") if isinstance(lineage, Mapping) else None
    )
    derivative_preview_binding = (
        lineage.get("derivative_preview") if isinstance(lineage, Mapping) else None
    )
    if (
        not isinstance(lineage, Mapping)
        or not isinstance(derivative_save_binding, Mapping)
        or not isinstance(derivative_preview_binding, Mapping)
        or path_id(derivative_save, contract="lower")
        != derivative_save_binding.get("path_id_lower")
        or path_id(derivative_preview, contract="lower")
        != derivative_preview_binding.get("path_id_lower")
    ):
        raise WorkflowError("committed derivative archive path identity is invalid")
    verify_restored_state(
        roots,
        manifest,
        source_save,
        loader_save,
        derivative_save,
        derivative_preview,
        runtime_binding,
        runtime_config,
        final_root,
        normalized(args.startup_consumption_bundle),
        expected_startup_evidence_hash=args.startup_consumption_evidence_hash,
        expected_validation_process_id=args.validation_process_id,
        expected_shutdown_evidence_hash=args.shutdown_evidence_hash,
    )
    require_processes_stopped(process_counts(args, roots))
    _verify_current_save_set_against_pre_boundary(args, roots, manifest)


def _fresh_revalidate_finalize_result(
    args: argparse.Namespace,
    roots: Roots,
    final_root: pathlib.Path,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    runtime_binding: Mapping[str, Any],
    runtime_binding_sha256: str,
    runtime_config: RuntimeConfigPreflight,
    *,
    source_recovery_sha256: Optional[str],
    source_recovery_state: str,
    supplied_candidate: Optional[Mapping[str, Any]],
    verify_committed_custody: Callable[[], None],
    verify_source_recovery_stable: Callable[[], None],
) -> tuple[dict[str, Any], pathlib.Path]:
    """Freshly prove restored state; no stored recovery field authorizes this path."""

    verify_source_recovery_stable()
    verify_committed_custody()
    _verify_committed_restored_state(
        args,
        roots,
        manifest,
        runtime_binding,
        runtime_config,
        final_root,
    )
    (
        attempt,
        report_root,
        attempt_comparison,
        final_root_identity,
        base_identity,
        attempt_identity,
    ) = _new_finalize_revalidation_attempt(roots, final_root)
    try:
        returned_report = run_post_boundary(
            args,
            roots,
            final_root,
            manifest,
            boundary_report_root=report_root,
            comparison_path=attempt_comparison,
        )
    except Exception:
        _verify_finalize_revalidation_directories(
            final_root,
            attempt,
            final_root_identity,
            base_identity,
            attempt_identity,
        )
        raise
    _verify_finalize_revalidation_directories(
        final_root,
        attempt,
        final_root_identity,
        base_identity,
        attempt_identity,
    )
    verify_source_recovery_stable()
    verify_committed_custody()
    _verify_committed_restored_state(
        args,
        roots,
        manifest,
        runtime_binding,
        runtime_config,
        final_root,
    )
    fresh_report, fresh_report_sha256 = _validated_post_boundary_report(
        roots,
        final_root,
        manifest,
        report_path=attempt_comparison,
    )
    returned_raw = json.dumps(
        returned_report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if (
        fresh_report != returned_report
        or fresh_report_sha256 != sha256_bytes(returned_raw)
    ):
        raise WorkflowError(
            "fresh post-boundary comparison changed after in-process authorization"
        )
    frozen_report_path = final_root / "revalidation-comparison.json"
    frozen_preexisted = os.path.lexists(frozen_report_path)
    if not frozen_preexisted:
        write_json_once_by_rename(
            frozen_report_path,
            fresh_report,
            "finalize recovery post-boundary comparison",
        )
    frozen_report, frozen_report_sha256 = _validated_post_boundary_report(
        roots,
        final_root,
        manifest,
        report_path=frozen_report_path,
    )
    if (
        (not frozen_preexisted and (
            frozen_report != fresh_report
            or frozen_report_sha256 != fresh_report_sha256
        ))
        or _post_boundary_semantic_projection(frozen_report)
        != _post_boundary_semantic_projection(fresh_report)
    ):
        raise WorkflowError(
            "fresh finalize revalidation semantics conflict with the frozen passing proof"
        )
    candidate_report_path = frozen_report_path
    if supplied_candidate is not None:
        _validate_finalize_document_hash(
            supplied_candidate,
            FINALIZE_RESULT_KEYS,
            "supplied finalize recovery candidate",
        )
        supplied_report_path = _result_post_boundary_report_path(
            roots,
            final_root,
            manifest,
            supplied_candidate,
            manifest_hash=manifest_hash,
            runtime_binding_sha256=runtime_binding_sha256,
            source_recovery_sha256=source_recovery_sha256,
            source_recovery_state=source_recovery_state,
        )
        supplied_report, _ = _validated_post_boundary_report(
            roots,
            final_root,
            manifest,
            report_path=supplied_report_path,
        )
        reconstructed_supplied = _expected_finalize_result(
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            runtime_binding_sha256,
            runtime_config,
            post_boundary_report_path=supplied_report_path,
        )
        if (
            dict(supplied_candidate) != reconstructed_supplied
            or _post_boundary_semantic_projection(supplied_report)
            != _post_boundary_semantic_projection(fresh_report)
        ):
            raise WorkflowError(
                "stored recovery candidate differs from fresh restored-state semantics"
            )
        candidate = copy.deepcopy(dict(supplied_candidate))
        candidate_report_path = supplied_report_path
    else:
        candidate = _expected_finalize_result(
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            runtime_binding_sha256,
            runtime_config,
            post_boundary_report_path=frozen_report_path,
        )
    transition_path = final_root / "revalidation-passed.json"
    transition = _seal_finalize_document(
        {
            "schema": FINALIZE_REVALIDATION_SCHEMA,
            "status": "passed",
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "runtime_binding_sha256": runtime_binding_sha256,
            "source_recovery_sha256": source_recovery_sha256,
            "source_recovery_state": source_recovery_state,
            "post_boundary_report_sha256": frozen_report_sha256,
            "candidate_result": candidate,
            "candidate_result_hash": contract_canonical_hash(candidate),
        }
    )
    write_json_once_by_rename(
        transition_path,
        transition,
        "finalize revalidation transition",
    )
    _validate_finalize_revalidation_transition(
        transition_path,
        roots=roots,
        manifest_hash=manifest_hash,
        runtime_binding_sha256=runtime_binding_sha256,
        source_recovery_sha256=source_recovery_sha256,
        source_recovery_state=source_recovery_state,
        report_sha256=frozen_report_sha256,
        candidate_result=candidate,
    )
    verify_source_recovery_stable()
    verify_committed_custody()
    _verify_committed_restored_state(
        args,
        roots,
        manifest,
        runtime_binding,
        runtime_config,
        final_root,
    )
    rebuilt = _expected_finalize_result(
        roots,
        final_root,
        manifest,
        manifest_hash,
        runtime_binding,
        runtime_binding_sha256,
        runtime_config,
        post_boundary_report_path=candidate_report_path,
    )
    _verify_finalize_revalidation_directories(
        final_root,
        attempt,
        final_root_identity,
        base_identity,
        attempt_identity,
    )
    if rebuilt != candidate:
        raise WorkflowError("fresh finalize revalidation changed before publication")
    _verify_committed_restored_state(
        args,
        roots,
        manifest,
        runtime_binding,
        runtime_config,
        final_root,
    )
    verify_source_recovery_stable()
    return candidate, candidate_report_path


def _result_post_boundary_report_path(
    roots: Roots,
    final_root: pathlib.Path,
    manifest: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    manifest_hash: str,
    runtime_binding_sha256: str,
    source_recovery_sha256: Optional[str],
    source_recovery_state: str,
) -> pathlib.Path:
    expected_hash = require_hash(
        result.get("post_boundary_report_sha256"),
        "finalize result post-boundary report hash",
    )
    standard = final_root / "post-boundary-comparison.json"
    revalidated = final_root / "revalidation-comparison.json"
    matching: list[pathlib.Path] = []
    for path in (standard, revalidated):
        if not os.path.lexists(path):
            continue
        if sha256_file(path) != expected_hash:
            continue
        _validated_post_boundary_report(
            roots,
            final_root,
            manifest,
            report_path=path,
        )
        matching.append(path)
    if not matching:
        raise WorkflowError("finalize result has no exact passing post-boundary report")
    if any(same_path(path, revalidated) for path in matching):
        _validate_finalize_revalidation_transition(
            final_root / "revalidation-passed.json",
            roots=roots,
            manifest_hash=manifest_hash,
            runtime_binding_sha256=runtime_binding_sha256,
            source_recovery_sha256=source_recovery_sha256,
            source_recovery_state=source_recovery_state,
            report_sha256=expected_hash,
            candidate_result=result,
        )
        return revalidated
    return standard


def recover_finalized_result(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
) -> Optional[dict[str, Any]]:
    final_root = roots.environment_dir / "finalization"
    result_path = final_root / "finalize-result.json"
    recovery_path = final_root / "finalize-recovery.json"
    transaction_recovery_path = final_root / "transaction-recovery.json"
    transaction_plan_path = final_root / "transaction-plan.json"
    if os.path.lexists(transaction_recovery_path):
        raise WorkflowError(
            "finalize transaction recovery latch is present; physical outcome is unknown"
        )
    recovery: Optional[dict[str, Any]]
    recovery_sha256: Optional[str]
    if recovery_path.exists():
        recovery, recovery_sha256 = load_json_with_sha256(
            recovery_path,
            "finalize recovery",
        )
    else:
        recovery = None
        recovery_sha256 = None
    candidate = (
        recovery.get("candidate_result")
        if isinstance(recovery, Mapping) and isinstance(recovery.get("candidate_result"), Mapping)
        else None
    )
    binding_path = roots.environment_dir / "runtime-binding.json"
    if recovery is None and not result_path.exists() and not binding_path.exists():
        if os.path.lexists(transaction_plan_path):
            raise WorkflowError("finalize transaction plan exists without its runtime binding")
        verify_runtime_config_frozen_copy(roots, runtime_config, required=False)
        return None

    if not binding_path.exists():
        raise WorkflowError("committed finalization is missing its runtime binding")
    runtime_binding = load_json(binding_path, "runtime binding")
    runtime_binding_sha256 = sha256_file(binding_path)
    if (
        set(runtime_binding) != RUNTIME_BINDING_KEYS
        or runtime_binding.get("schema") != RUNTIME_BINDING_SCHEMA
        or runtime_binding.get("run_id") != roots.run_id
        or runtime_binding.get("environment_manifest_sha256") != manifest_hash
        or runtime_binding.get("runtime_config") != runtime_config.binding
    ):
        raise WorkflowError("committed runtime binding lost its runtime config anchor")
    if not os.path.lexists(transaction_plan_path):
        if recovery is not None or result_path.exists():
            raise WorkflowError("finalize result/recovery has no transaction plan")
        # runtime-binding.json is a precommit input.  A crash before the
        # create-only plan/start record is safe to resume from full preflight.
        verify_runtime_config_frozen_copy(roots, runtime_config, required=False)
        return None
    transaction_plan = _validate_finalize_transaction_plan(
        load_json(transaction_plan_path, "finalize transaction plan"),
        roots=roots,
        manifest_hash=manifest_hash,
        runtime_binding_sha256=runtime_binding_sha256,
    )
    transaction_plan_file_sha256 = sha256_file(transaction_plan_path)
    transaction_state = _classify_finalize_transaction_journal(
        roots,
        transaction_plan,
        transaction_plan_file_sha256,
    )
    if transaction_state in {"no_attempt", "clean_rollback"}:
        if recovery is not None or result_path.exists():
            raise WorkflowError(
                "finalize result/recovery conflicts with a noncommitted transaction epoch"
            )
        verify_runtime_config_frozen_copy(roots, runtime_config, required=False)
        return None
    if transaction_state != "physical_committed":
        raise WorkflowError("finalize transaction journal state is ambiguous")

    verify_runtime_config_frozen_copy(roots, runtime_config, required=True)
    startup_consumption = runtime_binding.get("startup_consumption")
    if not isinstance(startup_consumption, Mapping) or set(startup_consumption) != STARTUP_CUSTODY_KEYS:
        raise WorkflowError("committed runtime binding lost its startup consumption anchor")
    def verify_committed_custody() -> None:
        _verify_committed_startup_custody(
            roots,
            runtime_binding,
            runtime_config=runtime_config,
            expected_bundle_path=normalized(args.startup_consumption_bundle),
            expected_evidence_hash=args.startup_consumption_evidence_hash,
            expected_validation_process_id=args.validation_process_id,
            expected_shutdown_evidence_hash=args.shutdown_evidence_hash,
        )

    verify_committed_custody()
    candidate_states = {
        "finalize_result_publication_outcome_unknown",
        "finalize_result_published_journal_outcome_unknown",
    }
    no_candidate_states = {"post_boundary_or_publication_blocked_after_physical_commit"}
    if recovery is not None:
        _validate_finalize_document_hash(
            recovery,
            FINALIZE_RECOVERY_KEYS,
            "finalize recovery",
        )
        expected_candidate_hash = (
            contract_canonical_hash(candidate) if candidate is not None else None
        )
        if (
            recovery.get("schema") != FINALIZE_RECOVERY_SCHEMA
            or recovery.get("status") != "outcome_unknown"
            or recovery.get("run_id") != roots.run_id
            or recovery.get("environment_manifest_sha256") != manifest_hash
            or recovery.get("physical_commit_non_replay") is not True
            or recovery.get("recovery_required") is not True
            or not isinstance(recovery.get("error_type"), str)
            or not recovery.get("error_type")
            or recovery.get("runtime_binding_sha256") != runtime_binding_sha256
            or recovery.get("runtime_config") != runtime_config.binding
            or recovery.get("runtime_config_binding_hash") != runtime_config.binding_hash
            or recovery.get("runtime_config_evidence_sha256") != runtime_config.file_sha256
            or recovery.get("startup_binding_sha256")
            != runtime_config.startup_binding_file_sha256
            or recovery.get("expected_release_dll_sha256")
            != runtime_config.binding["release_dll"]["external_expected_sha256"]
            or recovery.get("normal_shutdown_evidence_hash")
            != require_hash(args.shutdown_evidence_hash, "normal shutdown evidence external hash")
            or recovery.get("startup_consumption_evidence_hash")
            != startup_consumption["evidence_hash"]
            or recovery.get("startup_certificate_projection_hash")
            != startup_consumption["certificate_hash"]
            or recovery.get("startup_bundle_custody_hash")
            != startup_consumption["bundle_custody_hash"]
            or recovery.get("validation_process_id")
            != startup_consumption["validation_process_id"]
            or recovery.get("startup_session_id") != startup_consumption["session_id"]
            or recovery.get("validation_process_creation_token")
            != startup_consumption["validation_process_creation_token"]
            or recovery.get("startup_archive_prefix_sha256")
            != startup_consumption["startup_archive_prefix_sha256"]
            or recovery.get("startup_archive_prefix_length")
            != startup_consumption["startup_archive_prefix_length"]
            or recovery.get("candidate_result_hash") != expected_candidate_hash
            or recovery.get("state") not in candidate_states | no_candidate_states
            or (
                recovery.get("state") in candidate_states
                and candidate is None
            )
            or (
                recovery.get("state") in no_candidate_states
                and candidate is not None
            )
        ):
            raise WorkflowError("finalize recovery evidence is invalid")
        # The journal phase latch is audit evidence only.  It is deliberately
        # not an authorization input: an offline truncation can leave a valid
        # shorter local hash chain.  Missing-result recovery below always runs
        # a new independent post-boundary capture and comparison.

    source_recovery_state = (
        str(recovery.get("state"))
        if recovery is not None
        else IMPLICIT_POST_COMMIT_REVALIDATION_STATE
    )
    if candidate is not None:
        _validate_finalize_document_hash(
            candidate,
            FINALIZE_RESULT_KEYS,
            "finalize recovery candidate",
        )
        candidate_report_path = _result_post_boundary_report_path(
            roots,
            final_root,
            manifest,
            candidate,
            manifest_hash=manifest_hash,
            runtime_binding_sha256=runtime_binding_sha256,
            source_recovery_sha256=recovery_sha256,
            source_recovery_state=source_recovery_state,
        )
        reconstructed_candidate = _expected_finalize_result(
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            runtime_binding_sha256,
            runtime_config,
            post_boundary_report_path=candidate_report_path,
        )
        if dict(candidate) != reconstructed_candidate:
            raise WorkflowError(
                "finalize recovery candidate is not the exact reconstructed committed result"
            )

    def verify_source_recovery_stable() -> None:
        if recovery_sha256 is None:
            if os.path.lexists(recovery_path):
                raise WorkflowError("a finalize recovery record appeared during fresh revalidation")
            return
        if not recovery_path.exists() or sha256_file(recovery_path) != recovery_sha256:
            raise WorkflowError("finalize recovery evidence changed during fresh revalidation")

    expected_report_path: Optional[pathlib.Path] = None
    expected_result: Optional[dict[str, Any]] = None

    def validate_result(result: Mapping[str, Any]) -> None:
        nonlocal expected_report_path, expected_result
        _validate_finalize_document_hash(
            result,
            FINALIZE_RESULT_KEYS,
            "finalize result",
        )
        expected_report_path = _result_post_boundary_report_path(
            roots,
            final_root,
            manifest,
            result,
            manifest_hash=manifest_hash,
            runtime_binding_sha256=runtime_binding_sha256,
            source_recovery_sha256=recovery_sha256,
            source_recovery_state=source_recovery_state,
        )
        expected_result = _expected_finalize_result(
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            runtime_binding_sha256,
            runtime_config,
            post_boundary_report_path=expected_report_path,
        )
        if dict(result) != expected_result:
            raise WorkflowError("finalize result is not the exact reconstructed committed result")

    def verify_recovered_return_state(result: Mapping[str, Any]) -> dict[str, Any]:
        if expected_report_path is None or expected_result is None:
            raise WorkflowError("recovered finalize result was not reconstructed")
        verify_source_recovery_stable()
        verify_committed_custody()
        _verify_committed_restored_state(
            args,
            roots,
            manifest,
            runtime_binding,
            runtime_config,
            final_root,
        )
        rebuilt = _expected_finalize_result(
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            runtime_binding_sha256,
            runtime_config,
            post_boundary_report_path=expected_report_path,
        )
        current = load_json(result_path, "finalize result")
        validate_result(current)
        if (
            rebuilt != expected_result
            or dict(current) != dict(result)
            or not _verify_finalize_terminal_journal(roots, result_path, current)
        ):
            raise WorkflowError(
                "recovered finalize result, custody, and terminal journal changed before return"
            )
        verify_committed_custody()
        _verify_committed_restored_state(
            args,
            roots,
            manifest,
            runtime_binding,
            runtime_config,
            final_root,
        )
        verify_source_recovery_stable()
        return current

    def append_recovered_terminal(result: Mapping[str, Any]) -> None:
        try:
            append_journal(
                roots.environment_dir / "restore-journal.jsonl",
                "environment_finalized_recovered",
                {
                    "finalize_result_sha256": sha256_file(result_path),
                    "state": "fresh_post_boundary_revalidated",
                    "normal_shutdown_evidence_hash": require_hash(
                        args.shutdown_evidence_hash,
                        "normal shutdown evidence external hash",
                    ),
                    "startup_consumption_evidence_hash": startup_consumption["evidence_hash"],
                    "startup_session_id": startup_consumption["session_id"],
                    "validation_process_creation_token": startup_consumption[
                        "validation_process_creation_token"
                    ],
                    "startup_archive_prefix_sha256": startup_consumption[
                        "startup_archive_prefix_sha256"
                    ],
                    "startup_archive_prefix_length": startup_consumption[
                        "startup_archive_prefix_length"
                    ],
                },
            )
        except Exception as exc:
            raise WorkflowError(
                "finalize result is immutable but terminal journal acknowledgement remains outcome_unknown"
            ) from exc
        if not _verify_finalize_terminal_journal(roots, result_path, result):
            raise WorkflowError("recovered finalize terminal journal acknowledgement is missing")
        verify_recovered_return_state(result)

    if result_path.exists():
        result = load_json(result_path, "finalize result")
        _validate_finalize_document_hash(
            result,
            FINALIZE_RESULT_KEYS,
            "finalize result",
        )
        if candidate is not None and dict(result) != dict(candidate):
            raise WorkflowError("existing finalize result conflicts with recovery evidence")
        verify_committed_custody()
        terminal_present = _verify_finalize_terminal_journal(
            roots,
            result_path,
            result,
        )
        journal_path = roots.environment_dir / "restore-journal.jsonl"
        journal_sha256 = sha256_file(journal_path)
        if not args.apply:
            return {
                "schema": "oni.live-validation-finalize-recovery-plan.v4",
                "status": "would_fresh_revalidate_existing_result",
                "run_id": roots.run_id,
                "minimum_write_count": 2,
                "fresh_post_boundary_required": True,
                "stored_result_authorizes_return": False,
                "terminal_journal_authorizes_return": False,
                "terminal_journal_present": terminal_present,
                "physical_transaction_replayed": False,
                "finalize_result_sha256": sha256_file(result_path),
            }
        verify_source_recovery_stable()
        fresh_result, fresh_report_path = _fresh_revalidate_finalize_result(
            args,
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            runtime_binding_sha256,
            runtime_config,
            source_recovery_sha256=recovery_sha256,
            source_recovery_state=source_recovery_state,
            supplied_candidate=result,
            verify_committed_custody=verify_committed_custody,
            verify_source_recovery_stable=verify_source_recovery_stable,
        )
        if fresh_result != result:
            raise WorkflowError(
                "published finalize result differs from fresh replay revalidation"
            )
        if sha256_file(journal_path) != journal_sha256:
            raise WorkflowError("finalize journal changed during fresh replay revalidation")
        expected_report_path = fresh_report_path
        expected_result = fresh_result
        validate_result(result)
        verify_source_recovery_stable()
        if not terminal_present:
            append_recovered_terminal(result)
        return verify_recovered_return_state(result)

    if not args.apply:
        return {
            "schema": "oni.live-validation-finalize-recovery-plan.v3",
            "status": "would_fresh_revalidate_committed_result",
            "run_id": roots.run_id,
            "minimum_write_count": 4,
            "fresh_post_boundary_required": True,
            "stored_candidate_authorizes_publication": False,
            "physical_transaction_replayed": False,
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "normal_shutdown_evidence_hash": require_hash(
                args.shutdown_evidence_hash,
                "normal shutdown evidence external hash",
            ),
            "startup_consumption_evidence_hash": startup_consumption["evidence_hash"],
            "startup_certificate_projection_hash": startup_consumption["certificate_hash"],
            "startup_bundle_custody_hash": startup_consumption["bundle_custody_hash"],
            "validation_process_id": startup_consumption["validation_process_id"],
            "startup_session_id": startup_consumption["session_id"],
            "validation_process_creation_token": startup_consumption[
                "validation_process_creation_token"
            ],
            "startup_archive_prefix_sha256": startup_consumption[
                "startup_archive_prefix_sha256"
            ],
            "startup_archive_prefix_length": startup_consumption[
                "startup_archive_prefix_length"
            ],
            "expected_release_dll_sha256": runtime_config.binding["release_dll"][
                "external_expected_sha256"
            ],
        }

    verify_source_recovery_stable()
    fresh_candidate, fresh_report_path = _fresh_revalidate_finalize_result(
        args,
        roots,
        final_root,
        manifest,
        manifest_hash,
        runtime_binding,
        runtime_binding_sha256,
        runtime_config,
        source_recovery_sha256=recovery_sha256,
        source_recovery_state=source_recovery_state,
        supplied_candidate=candidate,
        verify_committed_custody=verify_committed_custody,
        verify_source_recovery_stable=verify_source_recovery_stable,
    )
    verify_source_recovery_stable()
    expected_report_path = fresh_report_path
    expected_result = fresh_candidate
    verify_committed_custody()
    write_json_once_by_rename(
        result_path,
        fresh_candidate,
        "recovered finalize result",
    )
    result = load_json(result_path, "recovered finalize result")
    validate_result(result)
    if dict(result) != dict(fresh_candidate):
        raise WorkflowError("recovered finalize result publication is not exact")
    verify_source_recovery_stable()
    append_recovered_terminal(result)
    return verify_recovered_return_state(result)


def finalize_locked(
    args: argparse.Namespace,
    roots: Roots,
    *,
    after_shutdown_verify_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    before_pipeline_final_verify_hook: Optional[Callable[[pathlib.Path], None]] = None,
) -> dict[str, Any]:
    reject_test_only_finalize_options(
        args,
        test_root_present=roots.test_root is not None,
    )
    if before_pipeline_final_verify_hook is not None and roots.test_root is None:
        raise WorkflowError("pipeline verification hooks are allowed only with TestRoot")
    manifest_path, manifest, manifest_hash = load_environment_manifest(args, roots)
    source_save, loader_save = verify_base_bindings(args, roots, manifest)
    verify_backups(roots, manifest)
    offline_runtime_config = validate_runtime_config_evidence(
        args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=False,
    )
    recovered = recover_finalized_result(
        args,
        roots,
        manifest,
        manifest_hash,
        offline_runtime_config,
    )
    if recovered is not None:
        return recovered
    counts = process_counts(args, roots)
    require_processes_stopped(counts)
    bridge = roots.oni_root / BRIDGE_RELATIVE
    gate = bridge_gate(bridge, require_bridge=True)
    bootstrap_path, bootstrap, old_archive = validate_bootstrap(roots, manifest, bridge)
    (
        lineage_path,
        lineage,
        derivative_save,
        derivative_preview,
        pair_report_path,
        pair_report,
        commands,
    ) = validate_lineage_and_bundles(
        args,
        roots,
        bridge,
        normalized(args.pre_boundary),
        source_save,
        loader_save,
    )
    def verify_shutdown_again() -> dict[str, Any]:
        try:
            return verify_published_shutdown_evidence(
                evidence_path=normalized(args.shutdown_evidence),
                expected_evidence_hash=args.shutdown_evidence_hash,
                manifest_path=lineage_path,
                bridge_dir=bridge,
                pair_report_path=pair_report_path,
                expected_pair_report_hash=args.priority_pair_report_hash,
                expected_run_id=roots.run_id,
                forward_step=args.forward_step_id,
                inverse_step=args.inverse_step_id,
                command_ids=list(args.command_id or []),
                command_bundle_hashes=list(args.command_bundle_hash or []),
                startup_bundle_dir=normalized(args.startup_consumption_bundle),
                expected_startup_evidence_hash=args.startup_consumption_evidence_hash,
                expected_validation_process_id=args.validation_process_id,
            )
        except ShutdownEvidenceError as exc:
            raise WorkflowError(f"normal shutdown evidence is invalid: {exc.code}") from exc

    shutdown_evidence = verify_shutdown_again()
    shutdown_document = shutdown_evidence.get("document")
    if not isinstance(shutdown_document, Mapping):
        raise WorkflowError("normal shutdown evidence document is missing")
    lineage_origin = _shutdown_lineage_origin(shutdown_document, roots)
    origin_snapshot_at = _parse_aware_timestamp(
        lineage_origin.get("snapshot_ts"),
        "trusted startup origin snapshot",
    )
    if offline_runtime_config.configured_at > origin_snapshot_at:
        raise WorkflowError(
            "runtime_config_after_trusted_startup_origin: runtime configuration "
            "completed after the trusted startup origin"
        )
    startup_consumption = capture_startup_consumption_preflight(
        args,
        roots,
        bridge,
        lineage_path,
        shutdown_evidence,
        offline_runtime_config,
        old_archive,
    )
    if after_shutdown_verify_hook is not None:
        if roots.test_root is None:
            raise WorkflowError("shutdown verification hooks are allowed only with TestRoot")
        after_shutdown_verify_hook(shutdown_evidence)
    pipeline_gate, runtime_bridge_snapshot = command_pipeline_gate(
        bridge,
        expected_save_id_hash=sha256_file(derivative_save),
        frozen_pipeline=(
            pair_report.get("shutdown", {}).get("command_pipeline", {})
            if isinstance(pair_report.get("shutdown"), Mapping)
            else {}
        ),
        expected_final_tip=(
            pair_report.get("final_tip")
            if isinstance(pair_report.get("final_tip"), Mapping)
            else {}
        ),
        shutdown_document=shutdown_document,
        expected_run_id=roots.run_id,
        runtime_config=offline_runtime_config,
        startup_certificate=startup_consumption.certificate,
        before_final_bridge_tree_verify_hook=(
            before_pipeline_final_verify_hook
            if roots.test_root is not None
            else None
        ),
    )
    del bootstrap, lineage
    _verify_startup_bundle_custody(
        startup_consumption.path,
        bridge,
        startup_consumption.custody,
        "current",
    )
    verify_shutdown_snapshot_bindings(bridge, runtime_bridge_snapshot, shutdown_evidence)
    verify_command_snapshot_bindings(bridge, runtime_bridge_snapshot, commands)
    if verify_shutdown_again() != shutdown_evidence:
        raise WorkflowError("normal shutdown evidence changed across runtime bridge capture")
    shutdown_runtime_custody = capture_shutdown_runtime_custody(
        roots,
        bridge,
        shutdown_evidence,
        startup_consumption.certificate,
    )
    _reject_shutdown_runtime_duplicate_custody(
        shutdown_runtime_custody,
        old_archive,
        label="restorable old bridge",
    )
    runtime_config = validate_runtime_config_evidence(
        args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=True,
    )
    if (
        runtime_config.file_sha256 != offline_runtime_config.file_sha256
        or runtime_config.startup_binding_file_sha256
        != offline_runtime_config.startup_binding_file_sha256
        or runtime_config.binding != offline_runtime_config.binding
        or runtime_config.binding_hash != offline_runtime_config.binding_hash
    ):
        raise WorkflowError("runtime config evidence changed across finalizer preflight")
    current_certificate_hash = _cross_bind_startup_certificate(
        startup_consumption.certificate,
        roots=roots,
        runtime_config=runtime_config,
        bundle_path=startup_consumption.path,
        expected_evidence_hash=startup_consumption.evidence_hash,
        expected_validation_process_id=startup_consumption.validation_process_id,
        custody=startup_consumption.custody,
    )
    if current_certificate_hash != startup_consumption.certificate_hash:
        raise WorkflowError("startup certificate changed across runtime config verification")
    lineage_origin = _shutdown_lineage_origin(
        shutdown_document,
        roots,
        certificate=startup_consumption.certificate,
    )
    origin_snapshot_at = _parse_aware_timestamp(
        lineage_origin.get("snapshot_ts"),
        "trusted startup origin snapshot",
    )
    if runtime_config.configured_at > origin_snapshot_at:
        raise WorkflowError(
            "runtime_config_after_trusted_startup_origin: runtime configuration "
            "completed after the trusted startup origin"
        )
    runtime_binding = build_runtime_binding(
        roots,
        manifest_hash,
        runtime_config.binding,
        bootstrap_path,
        lineage_path,
        derivative_save,
        derivative_preview,
        loader_save,
        pair_report_path,
        pair_report,
        shutdown_evidence,
        shutdown_runtime_custody,
        startup_consumption,
        runtime_bridge_snapshot,
        commands,
        gate,
        pipeline_gate,
        counts,
    )
    if not args.apply:
        return {
            "schema": "oni.live-validation-finalize-plan.v1",
            "status": "would_finalize",
            "run_id": roots.run_id,
            "write_count": 0,
            "command_count": len(commands),
            "derivative_pair_count": 1,
            "post_compare_allowed_stem_count": 1,
            "command_pipeline_ready": True,
            "normal_shutdown_evidence": True,
            "startup_consumption_evidence_hash": startup_consumption.evidence_hash,
            "startup_certificate_projection_hash": startup_consumption.certificate_hash,
            "validation_process_id": startup_consumption.validation_process_id,
            "startup_session_id": runtime_binding["startup_consumption"]["session_id"],
            "validation_process_creation_token": runtime_binding["startup_consumption"][
                "validation_process_creation_token"
            ],
            "startup_archive_prefix_sha256": runtime_binding["startup_consumption"][
                "startup_archive_prefix_sha256"
            ],
            "startup_archive_prefix_length": runtime_binding["startup_consumption"][
                "startup_archive_prefix_length"
            ],
            "runtime_config_historical_verified": True,
            "runtime_config_ordering_verified": True,
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "expected_release_dll_sha256": runtime_config.binding["release_dll"][
                "external_expected_sha256"
            ],
        }

    environment_dir = roots.environment_dir
    journal = environment_dir / "restore-journal.jsonl"
    binding_path = environment_dir / "runtime-binding.json"
    if binding_path.exists():
        if load_json(binding_path, "runtime binding") != runtime_binding:
            raise WorkflowError("existing runtime binding conflicts with current runtime evidence")
    else:
        write_json_atomic(binding_path, runtime_binding, create_only=True)
    binding_hash = sha256_file(binding_path)
    append_journal(
        journal,
        "finalize_preflight_passed",
        {
            "runtime_binding_sha256": binding_hash,
            "command_count": len(commands),
            "nonterminal_queue_count": 0,
            "temporary_queue_file_count": 0,
            "command_pipeline_document_hash": pipeline_gate["document_hash"],
            "normal_shutdown_evidence_hash": runtime_binding["normal_shutdown_evidence"]["evidence_hash"],
            "startup_consumption_evidence_hash": startup_consumption.evidence_hash,
            "startup_certificate_projection_hash": startup_consumption.certificate_hash,
            "startup_bundle_custody_hash": startup_consumption.custody_hash,
            "validation_process_id": startup_consumption.validation_process_id,
            "startup_session_id": runtime_binding["startup_consumption"]["session_id"],
            "validation_process_creation_token": runtime_binding["startup_consumption"][
                "validation_process_creation_token"
            ],
            "startup_archive_prefix_sha256": runtime_binding["startup_consumption"][
                "startup_archive_prefix_sha256"
            ],
            "startup_archive_prefix_length": runtime_binding["startup_consumption"][
                "startup_archive_prefix_length"
            ],
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "expected_release_dll_sha256": runtime_config.binding["release_dll"][
                "external_expected_sha256"
            ],
        },
    )

    final_root = environment_dir / "finalization"
    final_root.mkdir(parents=True, exist_ok=True)
    # Evidence filenames are intentionally compact so the transaction does not
    # manufacture a legacy Win32 MAX_PATH failure around the 64-hex command id.
    frozen = final_root / "pair"
    copy_evidence_once(
        runtime_config.path,
        final_root / pathlib.Path(*RUNTIME_CONFIG_FROZEN_RELATIVE.parts),
        runtime_config.file_sha256,
    )
    verify_runtime_config_frozen_copy(roots, runtime_config, required=True)
    copy_evidence_once(
        pair_report_path,
        frozen / "report.json",
        runtime_binding["priority_pair_report"]["file_sha256"],
    )
    copy_evidence_once(
        shutdown_evidence["path"],
        frozen / "shutdown.json",
        runtime_binding["normal_shutdown_evidence"]["file_sha256"],
    )
    for item in commands:
        digest = hashlib.sha256(item["id"].encode("utf-8", errors="strict")).hexdigest()
        copy_evidence_once(
            item["bundle_path"],
            frozen / "b" / f"{digest}.json",
            item["bundle_file_sha256"],
        )
        copy_evidence_once(
            item["freeze_path"],
            frozen / "f" / f"{item['step_id']}.json",
            item["freeze_file_sha256"],
        )

    # The proof inputs have now crossed several copy/publication boundaries.
    # Rebuild them again and require the full bridge snapshot immediately before
    # the destructive restore transaction.  The post-move verify_tree below is
    # the corresponding end of this custody interval.
    if verify_shutdown_again() != shutdown_evidence:
        raise WorkflowError("normal shutdown evidence changed before restore transaction")
    _verify_startup_bundle_custody(
        startup_consumption.path,
        bridge,
        startup_consumption.custody,
        "pretransaction current",
    )
    pretransaction_runtime_config = validate_runtime_config_evidence(
        args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=True,
    )
    if (
        pretransaction_runtime_config.file_sha256 != runtime_config.file_sha256
        or pretransaction_runtime_config.binding != runtime_config.binding
        or pretransaction_runtime_config.binding_hash != runtime_config.binding_hash
    ):
        raise WorkflowError("runtime config evidence changed before restore transaction")
    verify_runtime_config_frozen_copy(roots, runtime_config, required=True)
    verify_tree(bridge, runtime_bridge_snapshot, "runtime bridge pretransaction")
    if old_archive is not None and os.path.lexists(
        old_archive / pathlib.Path(startup_consumption.relative)
    ):
        raise WorkflowError("old bridge gained duplicate startup bundle custody before restore")
    _reject_runtime_config_recovery_records(roots)

    operations = _finalize_file_operations(
        roots,
        manifest,
        source_save=source_save,
        loader_save=loader_save,
        derivative_save=derivative_save,
        derivative_preview=derivative_preview,
        final_root=final_root,
        bridge=bridge,
        old_archive=old_archive,
    )
    expected_transaction_plan = _finalize_transaction_plan_document(
        roots,
        manifest_hash,
        binding_hash,
        operations,
    )
    transaction_plan, transaction_plan_file_sha256 = (
        _publish_or_verify_finalize_transaction_plan(
            final_root / "transaction-plan.json",
            expected_transaction_plan,
            roots=roots,
            manifest_hash=manifest_hash,
            runtime_binding_sha256=binding_hash,
        )
    )
    attempt_id = "attempt-" + uuid.uuid4().hex
    transaction_journal_append = _append_finalize_transaction_journal(
        transaction_plan,
        transaction_plan_file_sha256,
        attempt_id,
    )
    fault_after = int(args.test_fail_after_step or 0)
    tx = FileTransaction(
        journal,
        final_root,
        fault_after=fault_after,
        journal_append=transaction_journal_append,
        recovery_context={
            "schema": FINALIZE_TRANSACTION_RECOVERY_SCHEMA,
            "finalize_transaction_id": transaction_plan["transaction_id"],
            "finalize_attempt_id": attempt_id,
            "finalize_plan_hash": transaction_plan["plan_hash"],
            "finalize_plan_file_sha256": transaction_plan_file_sha256,
        },
    )
    try:
        transaction_journal_append(
            journal,
            "restore_transaction_started",
            {
                "transaction_kind": "finalize_restore",
                "expected_step_count": len(transaction_plan["steps"]),
            },
        )
    except Exception:
        tx._publish_recovery_state(
            "attempt_start_journal_outcome_unknown",
            ["restore_transaction_started"],
        )
        raise
    try:
        _execute_finalize_file_operations(tx, operations)

        verify_restored_state(
            roots,
            manifest,
            source_save,
            loader_save,
            derivative_save,
            derivative_preview,
            runtime_binding,
            runtime_config,
            final_root,
            startup_consumption.path,
            expected_startup_evidence_hash=startup_consumption.evidence_hash,
            expected_validation_process_id=startup_consumption.validation_process_id,
            expected_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
        )
        tx.commit()
    except Exception:
        if not tx.committed:
            tx.rollback()
        raise

    try:
        comparison = run_post_boundary(args, roots, final_root, manifest)
        published_comparison, published_comparison_sha256 = (
            _validated_post_boundary_report(roots, final_root, manifest)
        )
        comparison_raw = json.dumps(
            comparison,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        if (
            published_comparison != comparison
            or published_comparison_sha256 != sha256_bytes(comparison_raw)
        ):
            raise PostValidationError(
                "post-boundary comparison changed after in-process authorization"
            )
        _verify_committed_restored_state(
            args,
            roots,
            manifest,
            runtime_binding,
            runtime_config,
            final_root,
        )
    except Exception as exc:
        recovery_path = publish_finalize_recovery(
            roots,
            manifest_hash,
            "post_boundary_or_publication_blocked_after_physical_commit",
            runtime_config=runtime_config,
            runtime_binding_sha256=binding_hash,
            startup_consumption=runtime_binding["startup_consumption"],
            normal_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
            error_type=type(exc).__name__,
        )
        try:
            append_post_boundary_recovery_latch(roots, recovery_path)
        except Exception as latch_exc:
            raise WorkflowError(
                "post-boundary failed after physical commit and its recovery latch is outcome_unknown"
            ) from latch_exc
        raise
    try:
        result = _expected_finalize_result(
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            binding_hash,
            runtime_config,
            expected_post_boundary_report=comparison,
        )
    except Exception as exc:
        recovery_path = publish_finalize_recovery(
            roots,
            manifest_hash,
            "post_boundary_or_publication_blocked_after_physical_commit",
            runtime_config=runtime_config,
            runtime_binding_sha256=binding_hash,
            startup_consumption=runtime_binding["startup_consumption"],
            normal_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
            error_type=type(exc).__name__,
        )
        try:
            append_post_boundary_recovery_latch(roots, recovery_path)
        except Exception as latch_exc:
            raise WorkflowError(
                "result reconstruction failed after physical commit and its recovery latch is outcome_unknown"
            ) from latch_exc
        raise WorkflowError(
            "post-boundary result reconstruction failed after physical commit; automatic convergence is forbidden"
        ) from exc
    def finalize_result_fault(_path: pathlib.Path) -> None:
        if args.test_finalize_result_fail_after_verify:
            raise WorkflowError("injected finalize-result post-verification failure")

    try:
        _verify_committed_restored_state(
            args,
            roots,
            manifest,
            runtime_binding,
            runtime_config,
            final_root,
        )
        _verify_committed_startup_custody(
            roots,
            runtime_binding,
            runtime_config=runtime_config,
            expected_bundle_path=startup_consumption.path,
            expected_evidence_hash=startup_consumption.evidence_hash,
            expected_validation_process_id=startup_consumption.validation_process_id,
            expected_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
        )
        write_json_once_by_rename(
            final_root / "finalize-result.json",
            result,
            "finalize result",
            after_verify_hook=finalize_result_fault,
        )
    except Exception as exc:
        publish_finalize_recovery(
            roots,
            manifest_hash,
            "finalize_result_publication_outcome_unknown",
            runtime_config=runtime_config,
            runtime_binding_sha256=binding_hash,
            startup_consumption=runtime_binding["startup_consumption"],
            normal_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
            candidate_result=result,
            error_type=type(exc).__name__,
        )
        raise WorkflowError("finalize result publication failed after physical commit; non-replay recovery published") from exc
    try:
        append_journal(
            journal,
            "environment_finalized",
            {
                "finalize_result_sha256": sha256_file(final_root / "finalize-result.json"),
                "post_boundary_status": "passed",
                "normal_shutdown_evidence_hash": shutdown_evidence["evidence_hash"],
                "startup_consumption_evidence_hash": startup_consumption.evidence_hash,
                "startup_session_id": runtime_binding["startup_consumption"]["session_id"],
                "validation_process_creation_token": runtime_binding["startup_consumption"][
                    "validation_process_creation_token"
                ],
                "startup_archive_prefix_sha256": runtime_binding["startup_consumption"][
                    "startup_archive_prefix_sha256"
                ],
                "startup_archive_prefix_length": runtime_binding["startup_consumption"][
                    "startup_archive_prefix_length"
                ],
            },
        )
        published_result = load_json(final_root / "finalize-result.json", "finalize result")
        _validate_finalize_document_hash(
            published_result,
            FINALIZE_RESULT_KEYS,
            "finalize result",
        )
        if dict(published_result) != result or not _verify_finalize_terminal_journal(
            roots,
            final_root / "finalize-result.json",
            published_result,
        ):
            raise WorkflowError("finalize result and terminal journal acknowledgement did not converge")
        _verify_committed_startup_custody(
            roots,
            runtime_binding,
            runtime_config=runtime_config,
            expected_bundle_path=startup_consumption.path,
            expected_evidence_hash=startup_consumption.evidence_hash,
            expected_validation_process_id=startup_consumption.validation_process_id,
            expected_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
        )
        _verify_committed_restored_state(
            args,
            roots,
            manifest,
            runtime_binding,
            runtime_config,
            final_root,
        )
        rebuilt_result = _expected_finalize_result(
            roots,
            final_root,
            manifest,
            manifest_hash,
            runtime_binding,
            binding_hash,
            runtime_config,
            expected_post_boundary_report=comparison,
        )
        final_published_result = load_json(
            final_root / "finalize-result.json",
            "finalize result",
        )
        if (
            rebuilt_result != result
            or final_published_result != result
            or not _verify_finalize_terminal_journal(
                roots,
                final_root / "finalize-result.json",
                final_published_result,
            )
        ):
            raise WorkflowError(
                "finalize result, custody, and terminal journal changed before return"
            )
        _verify_committed_startup_custody(
            roots,
            runtime_binding,
            runtime_config=runtime_config,
            expected_bundle_path=startup_consumption.path,
            expected_evidence_hash=startup_consumption.evidence_hash,
            expected_validation_process_id=startup_consumption.validation_process_id,
            expected_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
        )
        _verify_committed_restored_state(
            args,
            roots,
            manifest,
            runtime_binding,
            runtime_config,
            final_root,
        )
    except Exception as exc:
        publish_finalize_recovery(
            roots,
            manifest_hash,
            "finalize_result_published_journal_outcome_unknown",
            runtime_config=runtime_config,
            runtime_binding_sha256=binding_hash,
            startup_consumption=runtime_binding["startup_consumption"],
            normal_shutdown_evidence_hash=shutdown_evidence["evidence_hash"],
            candidate_result=result,
            error_type=type(exc).__name__,
        )
        raise WorkflowError("finalize result is immutable but journal acknowledgement is outcome_unknown") from exc
    return result


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    roots = resolve_roots(args)
    reject_test_only_finalize_options(
        args,
        test_root_present=roots.test_root is not None,
    )
    if not args.apply:
        return finalize_locked(args, roots)
    with exclusive_finalize_lock(roots):
        return finalize_locked(args, roots)


def _frozen_runtime_payload(record: Mapping[str, Any], label: str) -> bytes:
    """Return the exact UTF-8 payload already authenticated by config v3."""

    encoded = record.get("raw_utf8_base64")
    length = record.get("raw_byte_length")
    if not isinstance(encoded, str) or isinstance(length, bool) or not isinstance(length, int):
        raise WorkflowError(f"{label} frozen payload fields are invalid")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise WorkflowError(f"{label} frozen payload is not canonical base64") from exc
    if (
        base64.b64encode(payload).decode("ascii") != encoded
        or len(payload) != length
        or sha256_bytes(payload) != require_hash(record.get("raw_sha256"), f"{label} raw hash")
    ):
        raise WorkflowError(f"{label} frozen payload binding is invalid")
    return payload


def _validate_disabled_control_policy(document: Mapping[str, Any]) -> None:
    expected_keys = {
        "schema",
        "controls_enabled",
        "test_run_id",
        "allowed_operations",
        "allowed_save_hashes",
        "allowed_save_path_ids",
        "allowed_target_save_path_ids",
        "generated_at",
        "expires_at",
    }
    if (
        set(document) != expected_keys
        or document.get("schema") != "oni.validation-control-policy.v1"
        or document.get("controls_enabled") is not False
        or document.get("test_run_id") is not None
        or document.get("allowed_operations") != []
        or document.get("allowed_save_hashes") != []
        or document.get("allowed_save_path_ids") != []
        or document.get("allowed_target_save_path_ids") != []
        or document.get("expires_at") is not None
    ):
        raise WorkflowError("validation-control policy is not strictly disabled and unbound")
    _parse_aware_timestamp(document.get("generated_at"), "validation-control policy generated_at")


def _abort_event_is_forbidden(event: Any) -> bool:
    if not isinstance(event, str):
        return False
    normalized_event = event.strip().casefold()
    return (
        normalized_event in {
            "session_started",
            "state_snapshot",
            "command_ack",
            "save_checkpoint",
            "reload_checkpoint",
        }
        or normalized_event.startswith("command_")
        or normalized_event.startswith("save_")
        or normalized_event.startswith("validation_control")
        or normalized_event.endswith("_ack")
    )


def _abort_document_has_forbidden_evidence(value: Any) -> bool:
    if isinstance(value, list):
        return any(_abort_document_has_forbidden_evidence(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    if value.get("schema") == "oni.state.v2" or _abort_event_is_forbidden(value.get("event")):
        return True
    for key in ("command_id", "snapshot_id", "save_write_id", "ack_seq"):
        if value.get(key) not in (None, "", [], {}):
            return True
    session_id = value.get("session_id")
    if session_id not in (None, "", "waiting"):
        return True
    return any(_abort_document_has_forbidden_evidence(item) for item in value.values())


def _validate_abort_window(
    roots: Roots,
    runtime_config: RuntimeConfigPreflight,
    *,
    frozen_runtime_probe_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Prove the narrow pre-session/pre-action abort window and bind diagnostics.

    Once the physical transaction has moved the runtime Probe, callers may
    supply the already-bound preflight snapshot.  Bridge/session evidence is
    still rebuilt from disk; the override never substitutes for the initial
    full Probe validation performed immediately before the first mutation.
    """

    bridge = roots.oni_root / BRIDGE_RELATIVE
    gate = bridge_gate(bridge, require_bridge=True)
    runtime = runtime_config.document.get("runtime")
    if not isinstance(runtime, Mapping):
        raise WorkflowError("runtime config evidence has no runtime anchors")
    bootstrap_record = runtime.get("bootstrap")
    policy_record = runtime.get("initial_disabled_policy")
    installed_record = runtime.get("installed_probe")
    if not all(isinstance(item, Mapping) for item in (bootstrap_record, policy_record, installed_record)):
        raise WorkflowError("runtime config abort anchors are incomplete")

    exact_files = (
        (
            bridge / "v27-runtime-bootstrap.json",
            _frozen_runtime_payload(bootstrap_record, "abort bootstrap"),
            "runtime bootstrap",
        ),
        (
            bridge / "action-policy.json",
            _frozen_runtime_payload(policy_record, "abort initial action policy"),
            "initial action policy",
        ),
    )
    exact_states: dict[str, dict[str, Any]] = {}
    for path, expected_payload, label in exact_files:
        snapshot = _stable_file_snapshot(path, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
        if snapshot["bytes"] != expected_payload:
            raise WorkflowError(f"{label} is not the exact frozen pre-session payload")
        exact_states[label] = {
            "path_id_lower": path_id(path, contract="lower"),
            "sha256": snapshot["sha256"],
            "length": snapshot["length"],
        }

    control_policy_path = bridge / "validation-control-policy.json"
    control_snapshot = _stable_file_snapshot(
        control_policy_path, max_bytes=MAX_JSON_BYTES, collect_bytes=True
    )
    try:
        control_document = runtime_config_core.parse_json_payload(
            control_snapshot["bytes"], "validation-control policy"
        )
    except runtime_config_core.ConfigError as exc:
        raise WorkflowError("validation-control policy JSON is invalid") from exc
    _validate_disabled_control_policy(control_document)

    commands = bridge / "commands"
    if not commands.is_dir() or is_reparse(commands):
        raise WorkflowError("abort requires the complete safe command queue root")
    command_stage_counts: dict[str, int] = {}
    for stage in COMMAND_DIRS:
        directory = commands / stage
        if not directory.is_dir() or is_reparse(directory):
            raise WorkflowError("abort requires every command stage directory")
        entries = list(directory.iterdir())
        if entries:
            raise WorkflowError("any command-stage evidence disables pre-session abort")
        command_stage_counts[stage] = 0

    controls = bridge / "validation-controls"
    control_stage_counts: dict[str, int] = {}
    for stage in ("approved", "inflight", "done", "failed", "quarantine"):
        directory = controls / stage
        if not directory.is_dir() or is_reparse(directory):
            raise WorkflowError("abort requires every validation-control stage directory")
        entries = list(directory.iterdir())
        if entries:
            raise WorkflowError("any validation-control evidence disables pre-session abort")
        control_stage_counts[stage] = 0

    forbidden_path_markers = {
        "confirmed-command-evidence",
        "priority-command-freezes",
        "action-outcomes.json",
        "priority-pair-finalization.json",
        "shutdown-evidence.json",
        "acks.jsonl",
    }
    diagnostic_files: list[dict[str, Any]] = []
    for path in sorted(bridge.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(bridge)
        if is_reparse(path):
            raise WorkflowError("abort runtime bridge contains a reparse point")
        if path.is_dir():
            if relative.parts and relative.parts[0].casefold() == "sessions":
                if len(relative.parts) >= 2 and relative.parts[1].casefold() != "waiting":
                    raise WorkflowError("a concrete session directory disables pre-session abort")
            continue
        lowered_parts = {part.casefold() for part in relative.parts}
        if lowered_parts & forbidden_path_markers or "save-lineage" in lowered_parts:
            raise WorkflowError("command/session/lineage evidence disables pre-session abort")
        snapshot = _stable_file_snapshot(path, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
        payload = snapshot["bytes"]
        if path.suffix.casefold() == ".jsonl":
            for raw_line in payload.splitlines():
                if not raw_line.strip():
                    continue
                try:
                    record = json.loads(
                        raw_line.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
                    )
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise WorkflowError("abort diagnostic JSONL is invalid") from exc
                if _abort_document_has_forbidden_evidence(record):
                    raise WorkflowError("session/snapshot/save/ACK evidence disables pre-session abort")
        elif path.suffix.casefold() == ".json" and path.name not in {
            "v27-runtime-bootstrap.json",
            "action-policy.json",
            "validation-control-policy.json",
        }:
            try:
                document = runtime_config_core.parse_json_payload(
                    payload, f"abort diagnostic {relative.as_posix()}"
                )
            except runtime_config_core.ConfigError as exc:
                raise WorkflowError("abort diagnostic JSON is invalid") from exc
            if _abort_document_has_forbidden_evidence(document):
                raise WorkflowError("session/snapshot/save/ACK evidence disables pre-session abort")
        diagnostic_files.append(
            {
                "relative": relative.as_posix(),
                "sha256": snapshot["sha256"],
                "length": snapshot["length"],
            }
        )

    if frozen_runtime_probe_snapshot is None:
        probe = roots.oni_root / PROBE_RELATIVE
        installed_hashes = {
            "OniAiProbeMod.dll": installed_record.get("dll_sha256"),
            "mod.yaml": installed_record.get("manifest_sha256"),
            "mod_info.yaml": installed_record.get("info_sha256"),
        }
        for name, expected_hash in installed_hashes.items():
            current = _stable_file_snapshot(probe / name)
            if current["sha256"] != require_hash(
                expected_hash, f"abort installed Probe {name} hash"
            ):
                raise WorkflowError("installed Probe changed before abort")
        probe_snapshot = snapshot_tree(probe)
    else:
        probe_snapshot = copy.deepcopy(dict(frozen_runtime_probe_snapshot))
        if probe_snapshot.get("exists") is not True:
            raise WorkflowError("frozen abort runtime Probe snapshot is invalid")
    bridge_snapshot = snapshot_tree(bridge)
    return {
        "gate": gate,
        "exact_files": exact_states,
        "validation_control_policy": {
            "path_id_lower": path_id(control_policy_path, contract="lower"),
            "sha256": control_snapshot["sha256"],
            "length": control_snapshot["length"],
        },
        "command_stage_counts": command_stage_counts,
        "control_stage_counts": control_stage_counts,
        "diagnostic_files": diagnostic_files,
        "diagnostic_file_count": len(diagnostic_files),
        "runtime_bridge_snapshot": bridge_snapshot,
        "runtime_bridge_snapshot_hash": sha256_bytes(canonical_bytes(bridge_snapshot)),
        "runtime_probe_snapshot": probe_snapshot,
        "runtime_probe_snapshot_hash": sha256_bytes(canonical_bytes(probe_snapshot)),
    }


def _abort_config_classification(
    roots: Roots,
    manifest: Mapping[str, Any],
    runtime_config: RuntimeConfigPreflight,
) -> dict[str, Any]:
    desired = runtime_config.document.get("configs")
    if not isinstance(desired, Mapping):
        raise WorkflowError("runtime config desired-config evidence is missing")
    records: list[dict[str, Any]] = []
    configured_count = 0
    original_count = 0
    drift_count = 0
    for original in manifest["backup"]["configs"]:
        relative = str(original["relative"])
        current = optional_file_state(roots.oni_root / pathlib.Path(relative))
        original_state = {key: value for key, value in original.items() if key in {"exists", "state"}}
        desired_record = desired.get(relative) if isinstance(desired.get(relative), Mapping) else None
        desired_state = (
            {"exists": True, "state": dict(desired_record["after"])}
            if isinstance(desired_record, Mapping) and isinstance(desired_record.get("after"), Mapping)
            else None
        )
        if current == original_state:
            classification = "original_exact"
            original_count += 1
        elif desired_state is not None and current == desired_state:
            classification = "configured_exact"
            configured_count += 1
        else:
            classification = "runtime_drift_or_partial"
            drift_count += 1
        records.append(
            {
                "relative": relative,
                "classification": classification,
                "current": current,
            }
        )
    target_count = len(runtime_config_core.TARGET_CONFIG_RELATIVES)
    if configured_count == target_count and drift_count == 0:
        overall = "configured_exact"
    elif original_count == len(records):
        overall = "already_original_with_runtime_installation"
    else:
        overall = "partial_or_startup_loader_drift"
    return {
        "classification": overall,
        "configured_exact_count": configured_count,
        "original_exact_count": original_count,
        "runtime_drift_or_partial_count": drift_count,
        "records": records,
    }


def _abort_preflight_document(
    args: argparse.Namespace,
    roots: Roots,
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    window: Mapping[str, Any],
    config_state: Mapping[str, Any],
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
) -> dict[str, Any]:
    archive_root_snapshot = snapshot_tree(roots.oni_root / BRIDGE_ARCHIVE_RELATIVE)
    return {
        "schema": ABORT_PREFLIGHT_SCHEMA,
        "status": "preflight_passed",
        "run_id": roots.run_id,
        "environment_manifest_sha256": manifest_hash,
        "runtime_config_evidence_sha256": runtime_config.file_sha256,
        "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
        "runtime_config_binding_hash": runtime_config.binding_hash,
        "expected_release_dll_sha256": runtime_config.binding["release_dll"][
            "external_expected_sha256"
        ],
        "managed_assembly_binding": runtime_config.binding["managed_assembly_binding"],
        "source_save": {
            "path_id_lower": path_id(source_save, contract="lower"),
            "state": file_state(source_save),
        },
        "loader_save": {
            "path_id_lower": path_id(loader_save, contract="lower"),
            "state": file_state(loader_save),
        },
        "loader_preview": {
            "path_id_lower": path_id(loader_save.with_suffix(".png"), contract="lower"),
            "state": file_state(loader_save.with_suffix(".png")),
        },
        "configuration_state": dict(config_state),
        "runtime_bridge_snapshot_hash": window["runtime_bridge_snapshot_hash"],
        "runtime_probe_snapshot_hash": window["runtime_probe_snapshot_hash"],
        "archive_root_exists": bool(archive_root_snapshot.get("exists")),
        "archive_root_snapshot_hash": sha256_bytes(canonical_bytes(archive_root_snapshot)),
        "diagnostic_file_count": window["diagnostic_file_count"],
        "processes": dict(process_counts(args, roots)),
        "strict_pre_session_pre_action": True,
    }


def _verify_abort_frozen_config(
    roots: Roots,
    runtime_config: RuntimeConfigPreflight,
    *,
    required: bool,
) -> Optional[dict[str, Any]]:
    target = roots.environment_dir / "abort" / "config.json"
    if not os.path.lexists(target):
        if required:
            raise WorkflowError("abort frozen runtime config evidence is missing")
        return None
    source = _stable_file_snapshot(runtime_config.path, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
    frozen = _stable_file_snapshot(target, max_bytes=MAX_JSON_BYTES, collect_bytes=True)
    if (
        source["sha256"] != runtime_config.file_sha256
        or frozen["sha256"] != runtime_config.file_sha256
        or source["bytes"] != frozen["bytes"]
    ):
        raise WorkflowError("abort frozen runtime config evidence conflicts with its external anchor")
    return {"path_id_lower": path_id(target, contract="lower"), "file_sha256": frozen["sha256"]}


def _verify_abort_restored_state(
    roots: Roots,
    manifest: Mapping[str, Any],
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
    preflight: Mapping[str, Any],
    window: Mapping[str, Any],
    abort_root: pathlib.Path,
) -> None:
    bindings = manifest["bindings"]
    if file_state(source_save) != bindings["source_save"]["state"]:
        raise WorkflowError("immutable source save changed during abort")
    if file_state(source_save.with_suffix(".png")) != bindings["source_preview"]["state"]:
        raise WorkflowError("immutable source preview changed during abort")
    if file_state(loader_save) != bindings["loader_save"]["state"]:
        raise WorkflowError("loader save was not restored exactly during abort")
    if file_state(loader_save.with_suffix(".png")) != bindings["loader_preview"]["state"]:
        raise WorkflowError("loader preview was not restored exactly during abort")
    if file_state(abort_root / "x" / "ls") != preflight["loader_save"]["state"]:
        raise WorkflowError("abort did not preserve the pre-restore loader save")
    if (
        file_state(abort_root / "x" / "lp")
        != preflight["loader_preview"]["state"]
    ):
        raise WorkflowError("abort did not preserve the pre-restore loader preview")
    for record in manifest["backup"]["configs"]:
        current = optional_file_state(roots.oni_root / pathlib.Path(record["relative"]))
        expected = {key: value for key, value in record.items() if key in {"exists", "state"}}
        if current != expected:
            raise WorkflowError("abort config restoration is not exact")
    verify_tree(roots.oni_root / PROBE_RELATIVE, manifest["backup"]["probe"]["snapshot"], "abort restored Probe")
    verify_tree(roots.oni_root / BRIDGE_RELATIVE, manifest["backup"]["old_bridge"]["snapshot"], "abort restored old bridge")
    verify_tree(
        roots.oni_root / BRIDGE_ARCHIVE_RELATIVE,
        manifest["backup"]["bridge_archive_root"]["snapshot"],
        "abort restored bridge archive root",
    )
    verify_tree(abort_root / "rb", window["runtime_bridge_snapshot"], "abort archived runtime bridge")
    verify_tree(abort_root / "x" / "p", window["runtime_probe_snapshot"], "abort archived runtime Probe")


def _abort_path_reference(roots: Roots, path: pathlib.Path) -> dict[str, str]:
    path = normalized(path)
    domains = (("oni_root", roots.oni_root), ("environment_dir", roots.environment_dir))
    for domain, root in domains:
        if path_within(path, root):
            relative = pathlib.PurePosixPath(path.relative_to(root).as_posix())
            validate_relative_path(relative.as_posix(), "abort transaction path")
            return {"domain": domain, "relative": relative.as_posix()}
    raise WorkflowError("abort transaction path escaped its two trusted roots")


def _resolve_abort_path_reference(roots: Roots, value: Any) -> pathlib.Path:
    if not isinstance(value, Mapping) or set(value) != {"domain", "relative"}:
        raise WorkflowError("abort transaction path reference is malformed")
    domain = value.get("domain")
    base = roots.oni_root if domain == "oni_root" else roots.environment_dir if domain == "environment_dir" else None
    if base is None:
        raise WorkflowError("abort transaction path domain is invalid")
    relative = validate_relative_path(str(value.get("relative") or ""), "abort transaction path")
    path = normalized(base / pathlib.Path(*relative.parts))
    if not path_within(path, base):
        raise WorkflowError("abort transaction path reference escaped its root")
    return path


def _owner_after_mtime(owner: Mapping[str, Any], value: int) -> dict[str, Any]:
    result = copy.deepcopy(dict(owner))
    if result.get("kind") == "file":
        result["file"]["last_write_ns"] = int(value)
    elif result.get("kind") == "directory":
        tree = result["tree"]
        tree["root_last_write_ns"] = int(value)
        unsigned = dict(tree)
        unsigned.pop("tree_hash", None)
        tree["tree_hash"] = sha256_bytes(canonical_bytes(unsigned))
    else:
        raise WorkflowError("abort mtime owner kind is invalid")
    return result


def _transaction_owner_matches_except_mtime(
    path: pathlib.Path,
    expected: Mapping[str, Any],
) -> bool:
    """Match one exact transaction owner while ignoring only its root mtime."""

    try:
        current = capture_transaction_node(path)
    except (OSError, WorkflowError):
        return False
    if current.get("kind") != expected.get("kind"):
        return False
    current_copy = copy.deepcopy(dict(current))
    expected_copy = copy.deepcopy(dict(expected))
    if current_copy.get("kind") == "file":
        current_copy["file"].pop("last_write_ns", None)
        expected_copy["file"].pop("last_write_ns", None)
    elif current_copy.get("kind") == "directory":
        for value in (current_copy, expected_copy):
            value["tree"].pop("root_last_write_ns", None)
            value["tree"].pop("tree_hash", None)
    else:
        return False
    return current_copy == expected_copy


def _seal_abort_document(value: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    if hash_field in result:
        raise WorkflowError("abort document hash field was already populated")
    result[hash_field] = contract_canonical_hash(result)
    return result


def _validate_abort_owner_record(
    owner: Any,
    label: str,
    *,
    kind: str,
    expected_file: Optional[Mapping[str, Any]] = None,
    expected_tree: Optional[Mapping[str, Any]] = None,
    expected_tree_hash: Optional[str] = None,
) -> None:
    if not isinstance(owner, Mapping):
        raise WorkflowError(f"{label} owner is missing")
    identity = owner.get("identity")
    if (
        not isinstance(identity, list)
        or len(identity) != 3
        or any(isinstance(item, bool) or not isinstance(item, int) for item in identity)
    ):
        raise WorkflowError(f"{label} owner identity is invalid")
    if kind == "file":
        if set(owner) != {"kind", "identity", "file"} or owner.get("kind") != "file":
            raise WorkflowError(f"{label} file-owner shape is invalid")
        state = owner.get("file")
        if not isinstance(state, Mapping) or set(state) != {"length", "last_write_ns", "sha256"}:
            raise WorkflowError(f"{label} file-owner state is invalid")
        if any(
            isinstance(state.get(field), bool) or not isinstance(state.get(field), int) or int(state[field]) < 0
            for field in ("length", "last_write_ns")
        ):
            raise WorkflowError(f"{label} file-owner length/timestamp is invalid")
        require_hash(state.get("sha256"), f"{label} file-owner hash")
        if expected_file is not None and dict(state) != dict(expected_file):
            raise WorkflowError(f"{label} file-owner does not match immutable state")
        return
    if kind != "directory":
        raise WorkflowError(f"{label} owner kind is unsupported")
    if set(owner) != {"kind", "identity", "tree", "node_identities"} or owner.get("kind") != "directory":
        raise WorkflowError(f"{label} directory-owner shape is invalid")
    tree = owner.get("tree")
    nodes = owner.get("node_identities")
    if not isinstance(tree, Mapping) or not isinstance(nodes, list) or not nodes:
        raise WorkflowError(f"{label} directory-owner evidence is incomplete")
    if expected_tree is not None and dict(tree) != dict(expected_tree):
        raise WorkflowError(f"{label} directory-owner does not match immutable tree")
    if expected_tree_hash is not None and sha256_bytes(canonical_bytes(tree)) != expected_tree_hash:
        raise WorkflowError(f"{label} directory-owner tree hash is invalid")
    for node in nodes:
        if (
            not isinstance(node, list)
            or len(node) != 5
            or not isinstance(node[0], str)
            or node[1] not in {"file", "directory"}
            or any(isinstance(item, bool) or not isinstance(item, int) for item in node[2:])
        ):
            raise WorkflowError(f"{label} directory-owner node identity is invalid")


def _expected_abort_plan_layout(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    runtime_config: RuntimeConfigPreflight,
    preflight: Mapping[str, Any],
) -> list[dict[str, Any]]:
    config_state = preflight.get("configuration_state")
    config_records = config_state.get("records") if isinstance(config_state, Mapping) else None
    manifest_configs = manifest.get("backup", {}).get("configs") if isinstance(manifest.get("backup"), Mapping) else None
    if not isinstance(config_records, list) or not isinstance(manifest_configs, list):
        raise WorkflowError("abort preflight config-state evidence is incomplete")
    if [item.get("relative") if isinstance(item, Mapping) else None for item in config_records] != [
        item.get("relative") if isinstance(item, Mapping) else None for item in manifest_configs
    ]:
        raise WorkflowError("abort preflight config-state ordering is invalid")

    abort_root = roots.environment_dir / "abort"
    stage = abort_root / "s"
    archive = abort_root / "x"
    loader_save = normalized(args.loader_save)
    steps: list[dict[str, Any]] = []

    def move(
        source: pathlib.Path,
        target: pathlib.Path,
        label: str,
        *,
        owner_kind: str,
        expected_file: Optional[Mapping[str, Any]] = None,
        expected_tree: Optional[Mapping[str, Any]] = None,
        expected_tree_hash: Optional[str] = None,
    ) -> None:
        steps.append(
            {
                "kind": "move",
                "label": label,
                "source": _abort_path_reference(roots, source),
                "target": _abort_path_reference(roots, target),
                "owner_kind": owner_kind,
                "expected_file": copy.deepcopy(dict(expected_file)) if expected_file is not None else None,
                "expected_tree": copy.deepcopy(dict(expected_tree)) if expected_tree is not None else None,
                "expected_tree_hash": expected_tree_hash,
            }
        )

    move(
        loader_save,
        archive / "ls",
        "abort_archive_loader_save",
        owner_kind="file",
        expected_file=preflight["loader_save"]["state"],
    )
    move(
        loader_save.with_suffix(".png"),
        archive / "lp",
        "abort_archive_loader_preview",
        owner_kind="file",
        expected_file=preflight["loader_preview"]["state"],
    )
    move(
        stage / "ls",
        loader_save,
        "abort_restore_loader_save",
        owner_kind="file",
        expected_file=preflight["source_save"]["state"],
    )
    move(
        stage / "lp",
        loader_save.with_suffix(".png"),
        "abort_restore_loader_preview",
        owner_kind="file",
        expected_file=manifest["bindings"]["source_preview"]["state"],
    )
    for index, (record, current_record) in enumerate(zip(manifest_configs, config_records)):
        if not isinstance(record, Mapping) or not isinstance(current_record, Mapping):
            raise WorkflowError("abort plan config record is invalid")
        current = current_record.get("current")
        if not isinstance(current, Mapping) or not isinstance(current.get("exists"), bool):
            raise WorkflowError("abort preflight current-config state is invalid")
        live = roots.oni_root / pathlib.Path(str(record["relative"]))
        if current.get("exists") is True:
            state = current.get("state")
            if not isinstance(state, Mapping):
                raise WorkflowError("abort preflight present config has no state")
            move(
                live,
                archive / "c" / f"{index:02d}",
                f"abort_archive_current_config_{index:02d}",
                owner_kind="file",
                expected_file=state,
            )
        if record.get("exists") is True:
            move(
                stage / "c" / f"{index:02d}",
                live,
                f"abort_restore_config_{index:02d}",
                owner_kind="file",
                expected_file=record.get("state"),
            )

    backup = manifest["backup"]
    probe = roots.oni_root / PROBE_RELATIVE
    move(
        probe,
        archive / "p",
        "abort_archive_runtime_probe",
        owner_kind="directory",
        expected_tree_hash=str(preflight["runtime_probe_snapshot_hash"]),
    )
    if backup["probe"]["snapshot"]["exists"]:
        move(
            stage / "p",
            probe,
            "abort_restore_probe",
            owner_kind="directory",
            expected_tree=backup["probe"]["snapshot"],
        )
    bridge = roots.oni_root / BRIDGE_RELATIVE
    move(
        bridge,
        abort_root / "rb",
        "abort_archive_runtime_bridge",
        owner_kind="directory",
        expected_tree_hash=str(preflight["runtime_bridge_snapshot_hash"]),
    )
    old_archive: Optional[pathlib.Path] = None
    if backup["old_bridge"]["snapshot"]["exists"]:
        runtime = runtime_config.document.get("runtime")
        bootstrap_record = runtime.get("bootstrap") if isinstance(runtime, Mapping) else None
        try:
            bootstrap = runtime_config_core.decode_frozen_json_record(
                bootstrap_record if isinstance(bootstrap_record, Mapping) else {},
                "abort plan frozen bootstrap",
            )
        except runtime_config_core.ConfigError as exc:
            raise WorkflowError("abort plan frozen bootstrap is invalid") from exc
        old_archive_value = bootstrap.get("archived_previous_bridge")
        if not isinstance(old_archive_value, str) or not old_archive_value:
            raise WorkflowError("abort plan has no immutable old-Bridge archive path")
        old_archive = normalized(old_archive_value)
        if not path_within(old_archive, roots.oni_root / BRIDGE_ARCHIVE_RELATIVE):
            raise WorkflowError("abort plan old-Bridge archive escaped its root")
    archive_root = roots.oni_root / BRIDGE_ARCHIVE_RELATIVE
    archive_snapshot = backup["bridge_archive_root"]["snapshot"]
    if archive_snapshot["exists"]:
        if old_archive is not None:
            move(
                old_archive,
                bridge,
                "abort_restore_old_bridge",
                owner_kind="directory",
                expected_tree=backup["old_bridge"]["snapshot"],
            )
        steps.append(
            {
                "kind": "set_mtime",
                "label": "abort_restore_archive_root_timestamp",
                "path": _abort_path_reference(roots, archive_root),
                "value": int(archive_snapshot["root_last_write_ns"]),
                "owner_kind": "directory",
                "expected_tree_hash": str(preflight["archive_root_snapshot_hash"]),
            }
        )
    elif preflight.get("archive_root_exists") is True:
        archived_root = archive / "ar"
        move(
            archive_root,
            archived_root,
            "abort_archive_created_archive_root",
            owner_kind="directory",
            expected_tree_hash=str(preflight["archive_root_snapshot_hash"]),
        )
        if old_archive is not None:
            move(
                archived_root / old_archive.relative_to(archive_root),
                bridge,
                "abort_restore_old_bridge",
                owner_kind="directory",
                expected_tree=backup["old_bridge"]["snapshot"],
            )
    elif old_archive is not None:
        raise WorkflowError("abort plan old Bridge archive has no archive root")
    return steps


def _validate_abort_transaction_plan(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    preflight_sha256: str,
) -> dict[str, Any]:
    preflight_path = roots.environment_dir / "abort" / "preflight.json"
    preflight = load_json(preflight_path, "abort preflight")
    if sha256_file(preflight_path) != preflight_sha256 or set(preflight) != ABORT_PREFLIGHT_KEYS:
        raise WorkflowError("abort preflight shape/hash is invalid")
    if (
        preflight.get("runtime_config_evidence_sha256") != runtime_config.file_sha256
        or preflight.get("startup_binding_sha256")
        != runtime_config.startup_binding_file_sha256
        or preflight.get("runtime_config_binding_hash") != runtime_config.binding_hash
    ):
        raise WorkflowError("abort preflight lost its runtime/startup anchors")
    required = {
        "schema",
        "transaction_id",
        "run_id",
        "environment_manifest_sha256",
        "runtime_config_evidence_sha256",
        "startup_binding_sha256",
        "runtime_config_binding_hash",
        "expected_release_dll_sha256",
        "managed_assembly_binding",
        "source_save_owner",
        "source_preview_owner",
        "release_dll_owner",
        "managed_assembly_owners",
        "preflight_sha256",
        "steps",
        "plan_hash",
    }
    if set(plan) != required or plan.get("schema") != ABORT_TRANSACTION_PLAN_SCHEMA:
        raise WorkflowError("abort transaction plan schema/keys are invalid")
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or re.fullmatch(r"abort-[0-9a-f]{32}", transaction_id) is None:
        raise WorkflowError("abort transaction id is invalid")
    if (
        plan.get("run_id") != roots.run_id
        or plan.get("environment_manifest_sha256") != manifest_hash
        or plan.get("runtime_config_evidence_sha256") != runtime_config.file_sha256
        or plan.get("startup_binding_sha256")
        != runtime_config.startup_binding_file_sha256
        or plan.get("runtime_config_binding_hash") != runtime_config.binding_hash
        or plan.get("expected_release_dll_sha256")
        != runtime_config.binding["release_dll"]["external_expected_sha256"]
        or plan.get("managed_assembly_binding") != runtime_config.binding["managed_assembly_binding"]
        or plan.get("preflight_sha256") != preflight_sha256
        or plan.get("plan_hash") != contract_hash_without(plan, "plan_hash", "abort transaction plan")
    ):
        raise WorkflowError("abort transaction plan lost an immutable anchor")
    plan_publications = [
        entry
        for entry in verify_journal(roots.environment_dir / "restore-journal.jsonl")
        if entry.get("event") == "abort_transaction_plan_published"
        and isinstance(entry.get("data"), Mapping)
        and entry["data"].get("abort_transaction_id") == plan.get("transaction_id")
        and entry["data"].get("abort_plan_hash") == plan.get("plan_hash")
    ]
    if (
        len(plan_publications) != 1
        or plan_publications[0]["data"].get("preflight_sha256") != preflight_sha256
        or plan_publications[0]["data"].get("startup_binding_sha256")
        != runtime_config.startup_binding_file_sha256
    ):
        raise WorkflowError("abort transaction plan has no unique durable publication anchor")
    managed_owners = plan.get("managed_assembly_owners")
    if (
        not isinstance(plan.get("release_dll_owner"), Mapping)
        or not isinstance(managed_owners, Mapping)
        or set(managed_owners) != set(runtime_config_core.MANAGED_ASSEMBLY_NAMES)
    ):
        raise WorkflowError("abort transaction plan custody anchors are incomplete")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("abort transaction plan has no physical steps")
    expected_steps = _expected_abort_plan_layout(args, roots, manifest, runtime_config, preflight)
    if len(steps) != len(expected_steps):
        raise WorkflowError("abort transaction plan step count is not uniquely reconstructable")
    labels: set[str] = set()
    for index, (step, expected) in enumerate(zip(steps, expected_steps), start=1):
        if not isinstance(step, Mapping) or step.get("index") != index:
            raise WorkflowError("abort transaction plan step ordering is invalid")
        label = step.get("label")
        if not isinstance(label, str) or not label or label in labels:
            raise WorkflowError("abort transaction plan step label is invalid")
        labels.add(label)
        kind = step.get("kind")
        if kind == "move":
            if set(step) != {"index", "kind", "label", "source", "target", "owner"}:
                raise WorkflowError("abort move plan step keys are invalid")
            source = _resolve_abort_path_reference(roots, step.get("source"))
            target = _resolve_abort_path_reference(roots, step.get("target"))
            if same_path(source, target) or paths_overlap(source, target):
                raise WorkflowError("abort move plan source/target overlap")
            if (
                step.get("label") != expected["label"]
                or step.get("source") != expected["source"]
                or step.get("target") != expected["target"]
            ):
                raise WorkflowError("abort move plan path/ordering semantics changed")
            _validate_abort_owner_record(
                step.get("owner"),
                str(step.get("label")),
                kind=str(expected["owner_kind"]),
                expected_file=expected.get("expected_file"),
                expected_tree=expected.get("expected_tree"),
                expected_tree_hash=expected.get("expected_tree_hash"),
            )
        elif kind == "set_mtime":
            if set(step) != {"index", "kind", "label", "path", "initial_owner", "value"}:
                raise WorkflowError("abort mtime plan step keys are invalid")
            _resolve_abort_path_reference(roots, step.get("path"))
            if not isinstance(step.get("initial_owner"), Mapping) or int(step.get("value", -1)) < 0:
                raise WorkflowError("abort mtime plan ownership/value is invalid")
            if (
                step.get("label") != expected["label"]
                or step.get("path") != expected["path"]
                or step.get("value") != expected["value"]
            ):
                raise WorkflowError("abort mtime plan path/value semantics changed")
            _validate_abort_owner_record(
                step.get("initial_owner"),
                str(step.get("label")),
                kind=str(expected["owner_kind"]),
                expected_tree_hash=expected.get("expected_tree_hash"),
            )
        else:
            raise WorkflowError("abort transaction plan step kind is invalid")
    _validate_abort_owner_record(
        plan.get("source_save_owner"),
        "abort immutable source save",
        kind="file",
        expected_file=manifest["bindings"]["source_save"]["state"],
    )
    _validate_abort_owner_record(
        plan.get("source_preview_owner"),
        "abort immutable source preview",
        kind="file",
        expected_file=manifest["bindings"]["source_preview"]["state"],
    )
    source_save = normalized(args.source_save)
    if (
        not transaction_node_matches(source_save, plan["source_save_owner"])
        or not transaction_node_matches(source_save.with_suffix(".png"), plan["source_preview_owner"])
    ):
        raise WorkflowError("abort immutable source plan owner is not current")
    _validate_abort_owner_record(
        plan.get("release_dll_owner"),
        "abort Release DLL",
        kind="file",
    )
    if not transaction_node_matches(normalized(args.release_dll), plan["release_dll_owner"]):
        raise WorkflowError("abort Release DLL plan owner is not current")
    managed_dir = normalized(args.game_managed_dir)
    for name in runtime_config_core.MANAGED_ASSEMBLY_NAMES:
        _validate_abort_owner_record(
            managed_owners.get(name),
            f"abort Managed {name}",
            kind="file",
        )
        if not transaction_node_matches(managed_dir / name, managed_owners[name]):
            raise WorkflowError("abort Managed plan owner is not current")
    return copy.deepcopy(dict(plan))


def _append_abort_plan_move(
    roots: Roots,
    steps: list[dict[str, Any]],
    source: pathlib.Path,
    target: pathlib.Path,
    label: str,
    *,
    owner_path: Optional[pathlib.Path] = None,
) -> None:
    steps.append(
        {
            "index": len(steps) + 1,
            "kind": "move",
            "label": label,
            "source": _abort_path_reference(roots, source),
            "target": _abort_path_reference(roots, target),
            "owner": capture_transaction_node(owner_path if owner_path is not None else source),
        }
    )


def _append_abort_plan_mtime(
    roots: Roots,
    steps: list[dict[str, Any]],
    path: pathlib.Path,
    value: int,
    label: str,
) -> None:
    before = capture_transaction_node(path)
    steps.append(
        {
            "index": len(steps) + 1,
            "kind": "set_mtime",
            "label": label,
            "path": _abort_path_reference(roots, path),
            "initial_owner": before,
            "value": int(value),
        }
    )


def _publish_abort_transaction_latch(
    roots: Roots,
    *,
    transaction_id: Optional[str],
    plan_hash: Optional[str],
    state: str,
    failures: Sequence[str],
) -> pathlib.Path:
    path = roots.environment_dir / "abort" / "transaction-recovery.json"
    payload = {
        "schema": ABORT_TRANSACTION_RECOVERY_SCHEMA,
        "status": "outcome_unknown",
        "run_id": roots.run_id,
        "transaction_id": transaction_id,
        "transaction_plan_hash": plan_hash,
        "state": state,
        "failures": list(failures),
        "recovery_required": True,
    }
    write_json_once(path, payload, "abort transaction recovery")
    return path


def _prepare_abort_transaction_plan(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    preflight_sha256: str,
    source_save: pathlib.Path,
    loader_save: pathlib.Path,
    old_archive: Optional[pathlib.Path],
) -> dict[str, Any]:
    abort_root = roots.environment_dir / "abort"
    plan_path = abort_root / "transaction-plan.json"
    if os.path.lexists(plan_path):
        return _validate_abort_transaction_plan(
            args,
            roots,
            load_json(plan_path, "abort transaction plan"),
            manifest,
            manifest_hash,
            runtime_config,
            preflight_sha256,
        )
    if os.path.lexists(abort_root / "transaction-recovery.json"):
        raise WorkflowError("abort transaction recovery latch requires manual resolution")
    stage = abort_root / "s"
    if os.path.lexists(stage):
        _publish_abort_transaction_latch(
            roots,
            transaction_id=None,
            plan_hash=None,
            state="unplanned_staging_present",
            failures=["stage_without_plan"],
        )
        raise WorkflowError("abort staging exists without a durable transaction plan")
    stage.mkdir()
    try:
        copy_file_stable(source_save, stage / "ls")
        copy_file_stable(source_save.with_suffix(".png"), stage / "lp")
        for index, record in enumerate(manifest["backup"]["configs"]):
            if record["exists"]:
                copy_file_stable(
                    roots.environment_dir / pathlib.Path(record["backup_relative"]),
                    stage / "c" / f"{index:02d}",
                )
        probe_record = manifest["backup"]["probe"]
        if probe_record["snapshot"]["exists"]:
            copy_tree_exact(
                roots.environment_dir / pathlib.Path(probe_record["backup_relative"]),
                stage / "p",
                probe_record["snapshot"],
            )
    except Exception as exc:
        _publish_abort_transaction_latch(
            roots,
            transaction_id=None,
            plan_hash=None,
            state="staging_outcome_unknown",
            failures=[type(exc).__name__],
        )
        raise

    steps: list[dict[str, Any]] = []
    archive = abort_root / "x"
    _append_abort_plan_move(roots, steps, loader_save, archive / "ls", "abort_archive_loader_save")
    _append_abort_plan_move(
        roots,
        steps,
        loader_save.with_suffix(".png"),
        archive / "lp",
        "abort_archive_loader_preview",
    )
    _append_abort_plan_move(roots, steps, stage / "ls", loader_save, "abort_restore_loader_save")
    _append_abort_plan_move(
        roots,
        steps,
        stage / "lp",
        loader_save.with_suffix(".png"),
        "abort_restore_loader_preview",
    )
    for index, record in enumerate(manifest["backup"]["configs"]):
        current = roots.oni_root / pathlib.Path(record["relative"])
        if current.exists():
            _append_abort_plan_move(
                roots,
                steps,
                current,
                archive / "c" / f"{index:02d}",
                f"abort_archive_current_config_{index:02d}",
            )
        if record["exists"]:
            _append_abort_plan_move(
                roots,
                steps,
                stage / "c" / f"{index:02d}",
                current,
                f"abort_restore_config_{index:02d}",
            )
    probe = roots.oni_root / PROBE_RELATIVE
    if probe.exists():
        _append_abort_plan_move(roots, steps, probe, archive / "p", "abort_archive_runtime_probe")
    if manifest["backup"]["probe"]["snapshot"]["exists"]:
        _append_abort_plan_move(roots, steps, stage / "p", probe, "abort_restore_probe")
    bridge = roots.oni_root / BRIDGE_RELATIVE
    _append_abort_plan_move(roots, steps, bridge, abort_root / "rb", "abort_archive_runtime_bridge")
    archive_root = roots.oni_root / BRIDGE_ARCHIVE_RELATIVE
    archive_snapshot = manifest["backup"]["bridge_archive_root"]["snapshot"]
    if archive_snapshot["exists"]:
        if manifest["backup"]["old_bridge"]["snapshot"]["exists"]:
            if old_archive is None:
                raise WorkflowError("archived old Bridge disappeared before abort plan publication")
            _append_abort_plan_move(roots, steps, old_archive, bridge, "abort_restore_old_bridge")
        _append_abort_plan_mtime(
            roots,
            steps,
            archive_root,
            int(archive_snapshot["root_last_write_ns"]),
            "abort_restore_archive_root_timestamp",
        )
    elif archive_root.exists():
        archived_root = archive / "ar"
        _append_abort_plan_move(
            roots,
            steps,
            archive_root,
            archived_root,
            "abort_archive_created_archive_root",
        )
        if manifest["backup"]["old_bridge"]["snapshot"]["exists"]:
            if old_archive is None:
                raise WorkflowError("archived old Bridge disappeared before abort plan publication")
            relocated_old_archive = archived_root / old_archive.relative_to(archive_root)
            _append_abort_plan_move(
                roots,
                steps,
                relocated_old_archive,
                bridge,
                "abort_restore_old_bridge",
                owner_path=old_archive,
            )
    elif manifest["backup"]["old_bridge"]["snapshot"]["exists"]:
        raise WorkflowError("old Bridge archive exists without its archive root")
    plan = _seal_abort_document(
        {
            "schema": ABORT_TRANSACTION_PLAN_SCHEMA,
            "transaction_id": f"abort-{uuid.uuid4().hex}",
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "expected_release_dll_sha256": runtime_config.binding["release_dll"][
                "external_expected_sha256"
            ],
            "managed_assembly_binding": runtime_config.binding["managed_assembly_binding"],
            "source_save_owner": capture_transaction_node(source_save),
            "source_preview_owner": capture_transaction_node(source_save.with_suffix(".png")),
            "release_dll_owner": capture_transaction_node(normalized(args.release_dll)),
            "managed_assembly_owners": {
                name: capture_transaction_node(normalized(args.game_managed_dir) / name)
                for name in runtime_config_core.MANAGED_ASSEMBLY_NAMES
            },
            "preflight_sha256": preflight_sha256,
            "steps": steps,
        },
        "plan_hash",
    )
    write_json_atomic(plan_path, plan, create_only=True)
    _append_abort_transaction_journal(plan)(
        roots.environment_dir / "restore-journal.jsonl",
        "abort_transaction_plan_published",
        {
            "preflight_sha256": preflight_sha256,
            "step_count": len(steps),
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
        },
    )
    return _validate_abort_transaction_plan(
        args,
        roots,
        load_json(plan_path, "abort transaction plan"),
        manifest,
        manifest_hash,
        runtime_config,
        preflight_sha256,
    )


def _abort_transaction_events(roots: Roots, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    transaction_id = plan["transaction_id"]
    plan_hash = plan["plan_hash"]
    return [
        entry
        for entry in verify_journal(roots.environment_dir / "restore-journal.jsonl")
        if isinstance(entry.get("data"), Mapping)
        and entry["data"].get("abort_transaction_id") == transaction_id
        and entry["data"].get("abort_plan_hash") == plan_hash
    ]


def _abort_commit_entry(
    roots: Roots,
    plan: Mapping[str, Any],
    *,
    required: bool,
) -> Optional[dict[str, Any]]:
    commits = [
        entry
        for entry in _abort_transaction_events(roots, plan)
        if entry.get("event") == "restore_transaction_physical_committed"
    ]
    if len(commits) > 1:
        raise WorkflowError("abort transaction has duplicate physical-commit records")
    if required and not commits:
        raise WorkflowError("abort result has no durable physical-commit record")
    return commits[0] if commits else None


def _append_abort_transaction_journal(
    plan: Mapping[str, Any],
) -> Callable[[pathlib.Path, str, Mapping[str, Any]], Mapping[str, Any]]:
    labels = {
        str(step["label"]): int(step["index"])
        for step in plan["steps"]
        if isinstance(step, Mapping)
    }

    def append(path: pathlib.Path, event: str, data: Mapping[str, Any]) -> Mapping[str, Any]:
        enriched = dict(data)
        enriched["abort_transaction_id"] = plan["transaction_id"]
        enriched["abort_plan_hash"] = plan["plan_hash"]
        label = enriched.get("label")
        if isinstance(label, str) and label in labels:
            enriched["abort_step_index"] = labels[label]
        return append_journal(path, event, enriched)

    return append


def _abort_move_position(
    roots: Roots,
    step: Mapping[str, Any],
    plan: Optional[Mapping[str, Any]] = None,
) -> tuple[str, pathlib.Path, pathlib.Path]:
    source = _resolve_abort_path_reference(roots, step["source"])
    target = _resolve_abort_path_reference(roots, step["target"])
    owner = step["owner"]
    source_matches = os.path.lexists(source) and transaction_node_matches(source, owner)
    target_matches = os.path.lexists(target) and transaction_node_matches(target, owner)
    # A later source may live below an earlier move target.  Before that
    # ancestor move executes, the exact owner is still below the corresponding
    # earlier source.  Treat that location as this step's pre-execution source
    # position so a freshly published plan is not mistaken for a partial crash.
    if not source_matches and plan is not None:
        step_index = int(step.get("index", 0))
        for prior in plan.get("steps", []):
            if (
                not isinstance(prior, Mapping)
                or prior.get("kind") != "move"
                or int(prior.get("index", 0)) >= step_index
            ):
                continue
            prior_source = _resolve_abort_path_reference(roots, prior["source"])
            prior_target = _resolve_abort_path_reference(roots, prior["target"])
            if not path_within(source, prior_target, strict=True):
                continue
            alternate = prior_source / source.relative_to(prior_target)
            if os.path.lexists(alternate) and transaction_node_matches(alternate, owner):
                source_matches = True
                break
    # A later plan step may target a pathname that is still occupied by an
    # earlier step's source in the initial state.  Ownership, not generic target
    # existence, determines this step's position.
    if source_matches and not target_matches:
        return "source", source, target
    if target_matches and not source_matches:
        return "target", source, target
    return "ambiguous", source, target


def _abort_latest_mtime_before_owner(
    roots: Roots,
    plan: Mapping[str, Any],
    label: str,
) -> Optional[dict[str, Any]]:
    matches = [
        entry.get("data", {}).get("abort_mtime_before_owner")
        for entry in _abort_transaction_events(roots, plan)
        if entry.get("event") == "abort_mtime_before"
        and entry.get("data", {}).get("label") == label
        and isinstance(entry.get("data", {}).get("abort_mtime_before_owner"), Mapping)
    ]
    return copy.deepcopy(dict(matches[-1])) if matches else None


def _normalize_abort_plan_initial_mtimes(
    roots: Roots,
    plan: Mapping[str, Any],
    append: Callable[[pathlib.Path, str, Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    """Restore parent root mtimes after all reverse moves have stopped."""

    for step in plan["steps"]:
        if step.get("kind") != "set_mtime":
            continue
        label = str(step["label"])
        path = _resolve_abort_path_reference(roots, step["path"])
        expected = step["initial_owner"]
        if transaction_node_matches(path, expected):
            continue
        if not _transaction_owner_matches_except_mtime(path, expected):
            raise WorkflowError(
                "abort rollback parent state differs beyond root mtime"
            )
        current = capture_transaction_node(path)
        claim = roots.environment_dir / "abort" / "r" / f"p-{int(step['index']):03d}"
        claimed = claim_transaction_node(path, current, claim)
        original_mtime = int(
            expected["file"]["last_write_ns"]
            if expected.get("kind") == "file"
            else expected["tree"]["root_last_write_ns"]
        )
        os.utime(claimed, ns=(original_mtime, original_mtime))
        if not transaction_node_matches(claimed, expected) or os.path.lexists(path):
            raise WorkflowError(
                "abort rollback parent-mtime normalization is ambiguous"
            )
        os.rename(claimed, path)
        if not transaction_node_matches(path, expected):
            raise WorkflowError(
                "abort rollback parent-mtime normalization was not exact"
            )
        append(
            roots.environment_dir / "restore-journal.jsonl",
            "abort_crash_recovery_step",
            {"label": label, "status": "root_mtime_normalized"},
        )


def _recover_interrupted_abort_transaction(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    latch = roots.environment_dir / "abort" / "transaction-recovery.json"
    if os.path.lexists(latch):
        raise WorkflowError("abort transaction recovery latch requires manual resolution")
    if _abort_commit_entry(roots, plan, required=False) is not None:
        return None
    events = _abort_transaction_events(roots, plan)
    positions: list[str] = []
    for step in plan["steps"]:
        if step["kind"] == "move":
            position, _source, _target = _abort_move_position(roots, step, plan)
        else:
            path = _resolve_abort_path_reference(roots, step["path"])
            before_owner = _abort_latest_mtime_before_owner(roots, plan, str(step["label"]))
            if before_owner is None:
                position = "source"
            elif os.path.lexists(path) and transaction_node_matches(path, before_owner):
                position = "source"
            elif os.path.lexists(path) and (
                transaction_node_matches(path, step["initial_owner"])
                or _transaction_owner_matches_except_mtime(
                    path, step["initial_owner"]
                )
            ):
                position = "source"
            elif os.path.lexists(path) and transaction_node_matches(
                path,
                _owner_after_mtime(before_owner, int(step["value"])),
            ):
                position = "target"
            else:
                position = "ambiguous"
        positions.append(position)
    if all(position == "source" for position in positions):
        return None
    if not args.apply:
        return {
            "schema": "oni.live-validation-abort-crash-recovery-plan.v1",
            "status": "would_recover_interrupted_abort",
            "run_id": roots.run_id,
            "write_count": 0,
            "transaction_id": plan["transaction_id"],
            "transaction_plan_hash": plan["plan_hash"],
            "observed_target_owner_count": sum(position == "target" for position in positions),
            "ambiguous_step_count": sum(position == "ambiguous" for position in positions),
        }
    append = _append_abort_transaction_journal(plan)
    failures: list[str] = []
    append(
        roots.environment_dir / "restore-journal.jsonl",
        "abort_crash_recovery_started",
        {"step_count": len(plan["steps"]), "prior_event_count": len(events)},
    )
    for step in reversed(plan["steps"]):
        label = str(step["label"])
        try:
            if step["kind"] == "move":
                position, source, target = _abort_move_position(roots, step, plan)
                if position == "target":
                    claim = roots.environment_dir / "abort" / "r" / f"c-{int(step['index']):03d}"
                    claimed = claim_transaction_node(target, step["owner"], claim)
                    if os.path.lexists(source):
                        raise WorkflowError("abort crash recovery source was externally recreated")
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(claimed, source)
                    if not transaction_node_matches(source, step["owner"]):
                        raise WorkflowError("abort crash recovery move was not exact")
                elif position != "source":
                    raise WorkflowError("abort crash recovery move ownership is ambiguous")
            else:
                path = _resolve_abort_path_reference(roots, step["path"])
                before_owner = _abort_latest_mtime_before_owner(roots, plan, label)
                if before_owner is None:
                    continue
                after_owner = _owner_after_mtime(before_owner, int(step["value"]))
                if transaction_node_matches(path, after_owner):
                    claim = roots.environment_dir / "abort" / "r" / f"u-{int(step['index']):03d}"
                    claimed = claim_transaction_node(path, after_owner, claim)
                    before = before_owner
                    before_mtime = int(
                        before["file"]["last_write_ns"]
                        if before.get("kind") == "file"
                        else before["tree"]["root_last_write_ns"]
                    )
                    os.utime(claimed, ns=(before_mtime, before_mtime))
                    if not transaction_node_matches(claimed, before) or os.path.lexists(path):
                        raise WorkflowError("abort crash recovery mtime claim is ambiguous")
                    os.rename(claimed, path)
                    if not transaction_node_matches(path, before):
                        raise WorkflowError("abort crash recovery mtime was not exact")
                elif transaction_node_matches(
                    path, step["initial_owner"]
                ) or _transaction_owner_matches_except_mtime(
                    path, step["initial_owner"]
                ):
                    # An earlier recovery may already have restored the child
                    # layout.  Root mtime is normalized after all reverse
                    # moves, when parent-directory churn has stopped.
                    pass
                elif not transaction_node_matches(path, before_owner):
                    raise WorkflowError("abort crash recovery mtime ownership is ambiguous")
            append(
                roots.environment_dir / "restore-journal.jsonl",
                "abort_crash_recovery_step",
                {"label": label, "status": "restored"},
            )
        except Exception as exc:
            failures.append(f"{label}:{type(exc).__name__}")
    if not failures:
        try:
            _normalize_abort_plan_initial_mtimes(roots, plan, append)
        except Exception as exc:
            failures.append(f"abort_parent_mtime:{type(exc).__name__}")
    if failures:
        try:
            _publish_abort_transaction_latch(
                roots,
                transaction_id=str(plan["transaction_id"]),
                plan_hash=str(plan["plan_hash"]),
                state="crash_recovery_outcome_unknown",
                failures=failures,
            )
        finally:
            raise WorkflowError("abort crash recovery outcome_unknown; transaction latch retained")
    append(
        roots.environment_dir / "restore-journal.jsonl",
        "abort_crash_recovery_complete",
        {"restored_step_count": len(plan["steps"]), "physical_transaction_replayed": False},
    )
    return {
        "schema": "oni.live-validation-abort-crash-recovery-result.v1",
        "status": "recovered_to_pre_abort",
        "run_id": roots.run_id,
        "transaction_id": plan["transaction_id"],
        "transaction_plan_hash": plan["plan_hash"],
        "physical_transaction_replayed": False,
        "retry_required": True,
    }


def _execute_abort_transaction_plan(tx: FileTransaction, roots: Roots, plan: Mapping[str, Any]) -> None:
    append = _append_abort_transaction_journal(plan)
    for step in plan["steps"]:
        if step["kind"] == "move":
            source = _resolve_abort_path_reference(roots, step["source"])
            target = _resolve_abort_path_reference(roots, step["target"])
            if not transaction_node_matches(source, step["owner"]):
                raise WorkflowError(f"abort plan source ownership changed: {step['label']}")
            tx.move(source, target, str(step["label"]))
            if not transaction_node_matches(target, step["owner"]):
                raise WorkflowError(f"abort plan target ownership changed: {step['label']}")
        else:
            path = _resolve_abort_path_reference(roots, step["path"])
            before_owner = capture_transaction_node(path)
            if before_owner.get("identity") != step["initial_owner"].get("identity"):
                raise WorkflowError(f"abort mtime plan root identity changed: {step['label']}")
            append(
                tx.journal,
                "abort_mtime_before",
                {"label": str(step["label"]), "abort_mtime_before_owner": before_owner},
            )
            tx.set_mtime(path, int(step["value"]), str(step["label"]))
            if not transaction_node_matches(path, _owner_after_mtime(before_owner, int(step["value"]))):
                raise WorkflowError(f"abort mtime plan result changed: {step['label']}")


def _abort_final_state_document(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    loader_save: pathlib.Path,
) -> dict[str, Any]:
    abort_root = roots.environment_dir / "abort"

    def tree_summary(path: pathlib.Path) -> dict[str, Any]:
        tree = snapshot_tree(path)
        return {
            "exists": bool(tree.get("exists")),
            "tree_hash": tree.get("tree_hash"),
            "file_count": int(tree.get("file_count", 0)),
            "total_bytes": int(tree.get("total_bytes", 0)),
            "root_last_write_ns": tree.get("root_last_write_ns"),
        }

    return {
        "schema": "oni.live-validation-abort-final-state.v1",
        "loader_save": file_state(loader_save),
        "loader_preview": file_state(loader_save.with_suffix(".png")),
        "configs": [
            {
                "relative": record["relative"],
                "current": optional_file_state(roots.oni_root / pathlib.Path(record["relative"])),
            }
            for record in manifest["backup"]["configs"]
        ],
        "probe": tree_summary(roots.oni_root / PROBE_RELATIVE),
        "bridge": tree_summary(roots.oni_root / BRIDGE_RELATIVE),
        "bridge_archive_root": tree_summary(roots.oni_root / BRIDGE_ARCHIVE_RELATIVE),
        "runtime_bridge_archive": tree_summary(abort_root / "rb"),
        "runtime_probe_archive": tree_summary(abort_root / "x" / "p"),
        "archived_loader_save": file_state(abort_root / "x" / "ls"),
        "archived_loader_preview": file_state(abort_root / "x" / "lp"),
        "processes": dict(process_counts(args, roots)),
    }


def _publish_abort_result_intent(
    roots: Roots,
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    preflight_sha256: str,
    plan: Mapping[str, Any],
    config_state: Mapping[str, Any],
    window: Mapping[str, Any],
    manifest: Mapping[str, Any],
    args: argparse.Namespace,
    loader_save: pathlib.Path,
) -> dict[str, Any]:
    final_state = _abort_final_state_document(args, roots, manifest, loader_save)
    final_state_hash = contract_canonical_hash(final_state)
    path = roots.environment_dir / "abort" / "result-intent.json"
    if os.path.lexists(path):
        existing = _load_abort_result_intent(
            roots,
            manifest_hash,
            runtime_config,
            preflight_sha256,
            plan,
        )
        if (
            existing.get("configuration_state") != config_state["classification"]
            or existing.get("diagnostic_file_count") != window["diagnostic_file_count"]
            or existing.get("config_count") != len(manifest["backup"]["configs"])
            or existing.get("old_bridge_restored")
            != bool(manifest["backup"]["old_bridge"]["snapshot"]["exists"])
            or existing.get("final_state_hash") != final_state_hash
        ):
            raise WorkflowError("existing abort result intent conflicts with the restored state")
        return existing
    intent = _seal_abort_document(
        {
            "schema": ABORT_RESULT_INTENT_SCHEMA,
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "preflight_sha256": preflight_sha256,
            "transaction_id": plan["transaction_id"],
            "transaction_plan_hash": plan["plan_hash"],
            "configuration_state": config_state["classification"],
            "diagnostic_file_count": window["diagnostic_file_count"],
            "config_count": len(manifest["backup"]["configs"]),
            "old_bridge_restored": bool(manifest["backup"]["old_bridge"]["snapshot"]["exists"]),
            "final_state_hash": final_state_hash,
            "completed_at": now_iso(),
        },
        "intent_hash",
    )
    write_json_once(path, intent, "abort result intent")
    loaded = load_json(path, "abort result intent")
    if loaded != intent:
        raise WorkflowError("abort result intent publication is not exact")
    return intent


def _load_abort_result_intent(
    roots: Roots,
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    preflight_sha256: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    path = roots.environment_dir / "abort" / "result-intent.json"
    intent = load_json(path, "abort result intent")
    required = {
        "schema",
        "run_id",
        "environment_manifest_sha256",
        "runtime_config_evidence_sha256",
        "startup_binding_sha256",
        "runtime_config_binding_hash",
        "preflight_sha256",
        "transaction_id",
        "transaction_plan_hash",
        "configuration_state",
        "diagnostic_file_count",
        "config_count",
        "old_bridge_restored",
        "final_state_hash",
        "completed_at",
        "intent_hash",
    }
    if (
        set(intent) != required
        or intent.get("schema") != ABORT_RESULT_INTENT_SCHEMA
        or intent.get("run_id") != roots.run_id
        or intent.get("environment_manifest_sha256") != manifest_hash
        or intent.get("runtime_config_evidence_sha256") != runtime_config.file_sha256
        or intent.get("startup_binding_sha256")
        != runtime_config.startup_binding_file_sha256
        or intent.get("runtime_config_binding_hash") != runtime_config.binding_hash
        or intent.get("preflight_sha256") != preflight_sha256
        or intent.get("transaction_id") != plan["transaction_id"]
        or intent.get("transaction_plan_hash") != plan["plan_hash"]
        or intent.get("intent_hash") != contract_hash_without(intent, "intent_hash", "abort result intent")
    ):
        raise WorkflowError("abort result intent is invalid")
    return intent


def _abort_result_from_intent(intent: Mapping[str, Any], commit_entry: Mapping[str, Any]) -> dict[str, Any]:
    return _seal_abort_document(
        {
            "schema": ABORT_RESULT_SCHEMA,
            "status": "aborted",
            "run_id": intent["run_id"],
            "environment_manifest_sha256": intent["environment_manifest_sha256"],
            "runtime_config_evidence_sha256": intent["runtime_config_evidence_sha256"],
            "startup_binding_sha256": intent["startup_binding_sha256"],
            "runtime_config_binding_hash": intent["runtime_config_binding_hash"],
            "preflight_sha256": intent["preflight_sha256"],
            "configuration_state": intent["configuration_state"],
            "diagnostic_file_count": intent["diagnostic_file_count"],
            "loader_pair_restored": True,
            "config_count": intent["config_count"],
            "probe_tree_restored": True,
            "old_bridge_restored": intent["old_bridge_restored"],
            "runtime_diagnostics_archived": True,
            "physical_restore_committed": True,
            "transaction_id": intent["transaction_id"],
            "transaction_plan_hash": intent["transaction_plan_hash"],
            "final_state_hash": intent["final_state_hash"],
            "physical_commit_entry_hash": commit_entry["entry_hash"],
            "completed_at": intent["completed_at"],
        },
        "document_hash",
    )


def _validate_abort_result(
    result: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    if (
        set(result) != ABORT_RESULT_KEYS
        or result.get("document_hash") != contract_hash_without(result, "document_hash", "abort result")
        or dict(result) != dict(expected)
    ):
        raise WorkflowError("abort result is not the exact committed candidate")


def publish_abort_recovery(
    roots: Roots,
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    preflight_sha256: str,
    plan: Mapping[str, Any],
    commit_entry: Mapping[str, Any],
    state: str,
    *,
    candidate_result: Mapping[str, Any],
    error_type: str,
) -> pathlib.Path:
    recovery_path = roots.environment_dir / "abort" / "abort-recovery.json"
    payload = _seal_abort_document(
        {
            "schema": ABORT_RECOVERY_SCHEMA,
            "status": "outcome_unknown",
            "run_id": roots.run_id,
            "environment_manifest_sha256": manifest_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "preflight_sha256": preflight_sha256,
            "transaction_id": plan["transaction_id"],
            "transaction_plan_hash": plan["plan_hash"],
            "physical_commit_entry_hash": commit_entry["entry_hash"],
            "state": state,
            "physical_commit_non_replay": True,
            "recovery_required": True,
            "error_type": error_type,
            "candidate_result": dict(candidate_result),
            "candidate_result_hash": contract_canonical_hash(candidate_result),
        },
        "document_hash",
    )
    write_json_once(recovery_path, payload, "abort recovery")
    return recovery_path


def recover_aborted_result(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    plan: Optional[Mapping[str, Any]],
    loader_save: pathlib.Path,
) -> Optional[dict[str, Any]]:
    abort_root = roots.environment_dir / "abort"
    result_path = abort_root / "abort-result.json"
    recovery_path = abort_root / "abort-recovery.json"
    commit_entry = _abort_commit_entry(roots, plan, required=False) if plan is not None else None
    if not result_path.exists() and not recovery_path.exists() and commit_entry is None:
        _verify_abort_frozen_config(roots, runtime_config, required=False)
        return None
    if plan is None:
        raise WorkflowError("abort result/recovery exists without a durable transaction plan")
    _abort_external_anchor_guard(args, roots, manifest, manifest_hash, runtime_config, plan)
    _verify_abort_frozen_config(roots, runtime_config, required=True)
    preflight_path = abort_root / "preflight.json"
    preflight = load_json(preflight_path, "abort preflight")
    preflight_sha256 = sha256_file(preflight_path)
    if (
        preflight.get("schema") != ABORT_PREFLIGHT_SCHEMA
        or preflight.get("run_id") != roots.run_id
        or preflight.get("environment_manifest_sha256") != manifest_hash
        or preflight.get("runtime_config_evidence_sha256") != runtime_config.file_sha256
        or preflight.get("startup_binding_sha256")
        != runtime_config.startup_binding_file_sha256
        or preflight.get("runtime_config_binding_hash") != runtime_config.binding_hash
        or preflight.get("managed_assembly_binding") != runtime_config.binding["managed_assembly_binding"]
    ):
        raise WorkflowError("abort preflight lost its immutable anchors")
    if preflight_sha256 != plan["preflight_sha256"]:
        raise WorkflowError("abort transaction plan does not bind the current preflight")
    commit_entry = _abort_commit_entry(roots, plan, required=True)
    intent = _load_abort_result_intent(
        roots,
        manifest_hash,
        runtime_config,
        preflight_sha256,
        plan,
    )
    if contract_canonical_hash(_abort_final_state_document(args, roots, manifest, loader_save)) != intent[
        "final_state_hash"
    ]:
        raise WorkflowError("committed abort final state drifted before result convergence")
    expected_result = _abort_result_from_intent(intent, commit_entry)
    recovery = load_json(recovery_path, "abort recovery") if recovery_path.exists() else None
    candidate: Optional[Mapping[str, Any]] = None
    if recovery is not None:
        recovery_keys = {
            "schema",
            "status",
            "run_id",
            "environment_manifest_sha256",
            "runtime_config_evidence_sha256",
            "startup_binding_sha256",
            "runtime_config_binding_hash",
            "preflight_sha256",
            "transaction_id",
            "transaction_plan_hash",
            "physical_commit_entry_hash",
            "state",
            "physical_commit_non_replay",
            "recovery_required",
            "error_type",
            "candidate_result",
            "candidate_result_hash",
            "document_hash",
        }
        candidate = recovery.get("candidate_result") if isinstance(recovery.get("candidate_result"), Mapping) else None
        if (
            set(recovery) != recovery_keys
            or recovery.get("schema") != ABORT_RECOVERY_SCHEMA
            or recovery.get("run_id") != roots.run_id
            or recovery.get("environment_manifest_sha256") != manifest_hash
            or recovery.get("runtime_config_evidence_sha256") != runtime_config.file_sha256
            or recovery.get("startup_binding_sha256")
            != runtime_config.startup_binding_file_sha256
            or recovery.get("runtime_config_binding_hash") != runtime_config.binding_hash
            or recovery.get("preflight_sha256") != preflight_sha256
            or recovery.get("transaction_id") != plan["transaction_id"]
            or recovery.get("transaction_plan_hash") != plan["plan_hash"]
            or recovery.get("physical_commit_entry_hash") != commit_entry["entry_hash"]
            or recovery.get("physical_commit_non_replay") is not True
            or candidate is None
            or recovery.get("candidate_result_hash") != contract_canonical_hash(candidate)
            or recovery.get("document_hash") != contract_hash_without(recovery, "document_hash", "abort recovery")
        ):
            raise WorkflowError("abort recovery evidence is invalid")
        _validate_abort_result(candidate, expected_result)

    if result_path.exists():
        result = load_json(result_path, "abort result")
        _validate_abort_result(result, expected_result)
        if candidate is not None and dict(result) != dict(candidate):
            raise WorkflowError("abort result conflicts with non-replay recovery")
        return result
    if not args.apply:
        return {
            "schema": "oni.live-validation-abort-recovery-plan.v1",
            "status": "would_converge_committed_result",
            "run_id": roots.run_id,
            "write_count": 1,
            "physical_transaction_replayed": False,
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "transaction_id": plan["transaction_id"],
            "transaction_plan_hash": plan["plan_hash"],
        }
    write_json_atomic(result_path, expected_result, create_only=True)
    result = load_json(result_path, "recovered abort result")
    _validate_abort_result(result, expected_result)
    try:
        append_journal(
            roots.environment_dir / "restore-journal.jsonl",
            "environment_abort_result_recovered",
            {"abort_result_sha256": sha256_file(result_path)},
        )
    except Exception:
        pass
    return result


def _abort_external_anchor_guard(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    runtime_config: RuntimeConfigPreflight,
    plan: Optional[Mapping[str, Any]] = None,
) -> RuntimeConfigPreflight:
    if sha256_file(roots.environment_dir / "environment-manifest.json") != manifest_hash:
        raise WorkflowError("environment manifest changed during abort transaction")
    source = normalized(args.source_save)
    source_binding = manifest["bindings"]["source_save"]
    preview_binding = manifest["bindings"]["source_preview"]
    if (
        source_binding.get("path_id_lower") != path_id(source, contract="lower")
        or source_binding.get("path_id_upper") != path_id(source, contract="upper")
        or preview_binding.get("path_id_lower") != path_id(source.with_suffix(".png"), contract="lower")
        or preview_binding.get("path_id_upper") != path_id(source.with_suffix(".png"), contract="upper")
        or file_state(source) != source_binding.get("state")
        or file_state(source.with_suffix(".png")) != preview_binding.get("state")
    ):
        raise WorkflowError("immutable source save/preview changed during abort custody")
    require_processes_stopped(process_counts(args, roots))
    verify_backups(roots, manifest)
    current = validate_runtime_config_evidence(
        args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=False,
    )
    if (
        current.file_sha256 != runtime_config.file_sha256
        or current.startup_binding_file_sha256
        != runtime_config.startup_binding_file_sha256
        or current.binding != runtime_config.binding
        or current.binding_hash != runtime_config.binding_hash
    ):
        raise WorkflowError("runtime config, Release, or Managed binding changed during abort")
    if plan is not None:
        if (
            not transaction_node_matches(source, plan["source_save_owner"])
            or not transaction_node_matches(source.with_suffix(".png"), plan["source_preview_owner"])
        ):
            raise WorkflowError("immutable source identity changed during abort custody")
        if not transaction_node_matches(normalized(args.release_dll), plan["release_dll_owner"]):
            raise WorkflowError("Release DLL identity changed during abort custody")
        managed_dir = normalized(args.game_managed_dir)
        for name in runtime_config_core.MANAGED_ASSEMBLY_NAMES:
            if not transaction_node_matches(managed_dir / name, plan["managed_assembly_owners"][name]):
                raise WorkflowError("Managed assembly identity changed during abort custody")
    return current


def _abort_path_budget(
    roots: Roots,
    manifest: Mapping[str, Any],
    window: Mapping[str, Any],
    loader_save: pathlib.Path,
    old_archive: Optional[pathlib.Path],
) -> dict[str, Any]:
    abort_root = roots.environment_dir / "abort"
    stage = abort_root / "s"
    archive = abort_root / "x"
    paths: dict[str, pathlib.Path] = {
        "finalize_lock": roots.environment_dir / ".finalize.lock",
        "finalize_lock_release": roots.environment_dir / ".finalize.lock.release-ffffffffffffffff",
        "preflight": abort_root / "preflight.json",
        "frozen_config": abort_root / "config.json",
        "transaction_plan": abort_root / "transaction-plan.json",
        "result_intent": abort_root / "result-intent.json",
        "result": abort_root / "abort-result.json",
        "result_recovery": abort_root / "abort-recovery.json",
        "transaction_recovery": abort_root / "transaction-recovery.json",
        "stage_loader_save": stage / "ls",
        "stage_loader_preview": stage / "lp",
        "archived_loader_save": archive / "ls",
        "archived_loader_preview": archive / "lp",
        "runtime_bridge_archive": abort_root / "rb",
        "rollback_move_claim": abort_root / "r" / "m-999",
        "rollback_crash_claim": abort_root / "r" / "c-999",
        "rollback_mtime_claim": abort_root / "r" / "u-999",
        "loader_save": loader_save,
        "loader_preview": loader_save.with_suffix(".png"),
    }
    for index, record in enumerate(manifest["backup"]["configs"]):
        paths[f"stage_config_{index}"] = stage / "c" / f"{index:02d}"
        paths[f"archive_config_{index}"] = archive / "c" / f"{index:02d}"
        paths[f"live_config_{index}"] = roots.oni_root / pathlib.Path(record["relative"])
    tree_bindings = (
        ("stage_probe", stage / "p", manifest["backup"]["probe"]["snapshot"]),
        ("archive_probe", archive / "p", window["runtime_probe_snapshot"]),
        ("archive_bridge", abort_root / "rb", window["runtime_bridge_snapshot"]),
    )
    for label, root, snapshot in tree_bindings:
        paths[label] = root
        for entry in snapshot.get("entries") or []:
            relative = validate_relative_path(str(entry.get("relative") or ""), "abort path-budget tree")
            paths[f"{label}:{relative.as_posix()}"] = root / pathlib.Path(*relative.parts)
    if old_archive is not None:
        old_snapshot = manifest["backup"]["old_bridge"]["snapshot"]
        paths["old_bridge_archive_source"] = old_archive
        for entry in old_snapshot.get("entries") or []:
            relative = validate_relative_path(str(entry.get("relative") or ""), "abort old-Bridge path budget")
            paths[f"old_bridge_archive_source:{relative.as_posix()}"] = old_archive / pathlib.Path(*relative.parts)
    original_archive = manifest["backup"]["bridge_archive_root"]["snapshot"]
    current_archive_root = roots.oni_root / BRIDGE_ARCHIVE_RELATIVE
    if original_archive.get("exists") is not True and current_archive_root.exists():
        created_archive_snapshot = snapshot_tree(current_archive_root)
        archived_created_root = archive / "ar"
        paths["archive_created_root"] = archived_created_root
        for entry in created_archive_snapshot.get("entries") or []:
            relative = validate_relative_path(str(entry.get("relative") or ""), "abort created archive-root path budget")
            paths[f"archive_created_root:{relative.as_posix()}"] = archived_created_root / pathlib.Path(*relative.parts)
    committed = list(paths.items())
    for label, path in committed:
        paths[f"json_temp:{label}"] = path.parent / ".j-ffffffffffffffff.tmp"
        paths[f"copy_temp:{label}"] = path.parent / ".c-ffffffffffffffff.tmp"
        paths[f"tree_temp:{label}"] = path.parent / ".t-ffffffffffffffff.tmp"
        paths[f"cleanup:{label}"] = path.parent / f".{path.name[:24]}.cleanup-ffffffffffffffff"

    def utf16_units(path: pathlib.Path) -> int:
        return len(str(normalized(path)).encode("utf-16-le")) // 2 + 1

    measured = [(label, normalized(path), utf16_units(path)) for label, path in paths.items()]
    label, path, maximum = max(measured, key=lambda item: item[2])
    if maximum > MAX_LEGACY_PATH_UTF16_UNITS:
        raise WorkflowError(
            "abort path budget exceeds the legacy Win32 limit before publication: "
            f"{label} ({maximum} UTF-16 units including NUL)"
        )
    return {
        "contract": "win32-legacy-path-utf16-including-nul.v1",
        "limit": MAX_LEGACY_PATH_UTF16_UNITS,
        "max_utf16_units": maximum,
        "max_path_id_lower": path_id(path, contract="lower"),
        "enumerated_path_count": len(measured),
    }


def abort_locked(
    args: argparse.Namespace,
    roots: Roots,
    *,
    before_transaction_hook: Optional[Callable[[], None]] = None,
    transaction_after_step_hook: Optional[Callable[[int, str], None]] = None,
    transaction_after_physical_hook: Optional[Callable[[str, pathlib.Path], None]] = None,
    transaction_after_commit_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    transaction_before_commit_hook: Optional[Callable[[Mapping[str, Any]], None]] = None,
    transaction_before_undo_claim_hook: Optional[Callable[[str, pathlib.Path], None]] = None,
    transaction_before_publish_hook: Optional[Callable[[str, pathlib.Path], None]] = None,
) -> dict[str, Any]:
    hooks = (
        before_transaction_hook,
        transaction_after_step_hook,
        transaction_after_physical_hook,
        transaction_after_commit_hook,
        transaction_before_commit_hook,
        transaction_before_undo_claim_hook,
        transaction_before_publish_hook,
    )
    if any(hook is not None for hook in hooks) and roots.test_root is None:
        raise WorkflowError("abort transaction hooks are forbidden in live mode")
    reject_test_only_abort_options(args, test_root_present=roots.test_root is not None)
    _manifest_path, manifest, manifest_hash = load_environment_manifest(args, roots)
    expected_manifest_hash = require_hash(
        args.expected_environment_manifest_sha256,
        "external environment manifest anchor",
    )
    if manifest_hash != expected_manifest_hash:
        raise WorkflowError("environment manifest does not match its external SHA-256 anchor")
    runtime_config = validate_runtime_config_evidence(
        args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=False,
    )
    abort_root = roots.environment_dir / "abort"
    preflight_path = abort_root / "preflight.json"
    plan_path = abort_root / "transaction-plan.json"
    plan: Optional[dict[str, Any]] = None
    if os.path.lexists(plan_path):
        if not os.path.lexists(preflight_path):
            raise WorkflowError("abort transaction plan exists without immutable preflight")
        plan = _validate_abort_transaction_plan(
            args,
            roots,
            load_json(plan_path, "abort transaction plan"),
            manifest,
            manifest_hash,
            runtime_config,
            sha256_file(preflight_path),
        )
        interrupted = _recover_interrupted_abort_transaction(args, roots, plan)
        if interrupted is not None:
            return interrupted
    loader_save = normalized(args.loader_save)
    recovered = recover_aborted_result(
        args,
        roots,
        manifest,
        manifest_hash,
        runtime_config,
        plan,
        loader_save,
    )
    if recovered is not None:
        return recovered

    source_save, loader_save = verify_base_bindings(args, roots, manifest)
    verify_backups(roots, manifest)
    counts = process_counts(args, roots)
    require_processes_stopped(counts)
    bridge = roots.oni_root / BRIDGE_RELATIVE
    _bootstrap_path, _bootstrap, old_archive = validate_bootstrap(roots, manifest, bridge)
    window = _validate_abort_window(roots, runtime_config)
    config_state = _abort_config_classification(roots, manifest, runtime_config)
    fresh_runtime_config = validate_runtime_config_evidence(
        args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=True,
    )
    if (
        fresh_runtime_config.file_sha256 != runtime_config.file_sha256
        or fresh_runtime_config.startup_binding_file_sha256
        != runtime_config.startup_binding_file_sha256
        or fresh_runtime_config.binding != runtime_config.binding
        or fresh_runtime_config.binding_hash != runtime_config.binding_hash
    ):
        raise WorkflowError(
            "runtime config/startup binding changed across abort preflight"
        )
    preflight = _abort_preflight_document(
        args,
        roots,
        manifest_hash,
        runtime_config,
        window,
        config_state,
        source_save,
        loader_save,
    )
    path_budget = _abort_path_budget(roots, manifest, window, loader_save, old_archive)
    if not args.apply:
        return {
            "schema": "oni.live-validation-abort-plan.v1",
            "status": "would_abort",
            "run_id": roots.run_id,
            "write_count": 0,
            "strict_pre_session_pre_action": True,
            "configuration_state": config_state["classification"],
            "diagnostic_file_count": window["diagnostic_file_count"],
            "environment_manifest_sha256": manifest_hash,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "runtime_config_binding_hash": runtime_config.binding_hash,
            "expected_release_dll_sha256": runtime_config.binding["release_dll"][
                "external_expected_sha256"
            ],
            "path_budget": path_budget,
        }

    write_json_once(preflight_path, preflight, "abort preflight")
    preflight_sha256 = sha256_file(preflight_path)
    copy_evidence_once(runtime_config.path, abort_root / "config.json", runtime_config.file_sha256)
    _verify_abort_frozen_config(roots, runtime_config, required=True)
    journal = roots.environment_dir / "restore-journal.jsonl"
    append_journal(
        journal,
        "abort_preflight_passed",
        {
            "preflight_sha256": preflight_sha256,
            "runtime_config_evidence_sha256": runtime_config.file_sha256,
            "startup_binding_sha256": runtime_config.startup_binding_file_sha256,
            "configuration_state": config_state["classification"],
        },
    )
    if before_transaction_hook is not None:
        before_transaction_hook()

    # Rebuild every mutable guard after preflight publication and immediately
    # before the first physical restore operation.
    source_check, loader_check = verify_base_bindings(args, roots, manifest)
    if not same_path(source_check, source_save) or not same_path(loader_check, loader_save):
        raise WorkflowError("abort source/loader path binding changed before transaction")
    verify_backups(roots, manifest)
    current_runtime_config = validate_runtime_config_evidence(
        args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=True,
    )
    if (
        current_runtime_config.file_sha256 != runtime_config.file_sha256
        or current_runtime_config.startup_binding_file_sha256
        != runtime_config.startup_binding_file_sha256
        or current_runtime_config.binding != runtime_config.binding
        or current_runtime_config.binding_hash != runtime_config.binding_hash
    ):
        raise WorkflowError("runtime config or Managed binding changed before abort transaction")
    _bootstrap_path, _bootstrap, current_old_archive = validate_bootstrap(roots, manifest, bridge)
    if (old_archive is None) != (current_old_archive is None) or (
        old_archive is not None
        and current_old_archive is not None
        and not same_path(old_archive, current_old_archive)
    ):
        raise WorkflowError("archived old Bridge changed before abort transaction")
    current_window = _validate_abort_window(roots, runtime_config)
    current_config_state = _abort_config_classification(roots, manifest, runtime_config)
    current_preflight = _abort_preflight_document(
        args,
        roots,
        manifest_hash,
        runtime_config,
        current_window,
        current_config_state,
        source_save,
        loader_save,
    )
    if current_preflight != preflight:
        raise WorkflowError("abort preflight state changed before physical restore")

    try:
        plan = _prepare_abort_transaction_plan(
            args,
            roots,
            manifest,
            manifest_hash,
            runtime_config,
            preflight_sha256,
            source_save,
            loader_save,
            old_archive,
        )
    except Exception as exc:
        # Staging precedes the create-only plan publication.  A fault while
        # capturing plan owners or publishing the plan must therefore leave an
        # immediate, non-replay latch rather than relying on a later invocation
        # to notice the orphaned stage.
        stage = abort_root / "s"
        recovery = abort_root / "transaction-recovery.json"
        if os.path.lexists(stage) and not os.path.lexists(plan_path) and not os.path.lexists(recovery):
            _publish_abort_transaction_latch(
                roots,
                transaction_id=None,
                plan_hash=None,
                state="plan_publication_outcome_unknown",
                failures=[type(exc).__name__],
            )
        raise
    interrupted = _recover_interrupted_abort_transaction(args, roots, plan)
    if interrupted is not None:
        return interrupted

    # The external callback executes before the final guard so every TestRoot
    # late-race injection is observed before the first live pathname changes.
    first_publication = True
    runtime_bridge_archived = False

    def guarded_before_publish(label: str, target: pathlib.Path) -> None:
        nonlocal first_publication
        if transaction_before_publish_hook is not None:
            transaction_before_publish_hook(label, target)
        _abort_external_anchor_guard(args, roots, manifest, manifest_hash, runtime_config, plan)
        if not runtime_bridge_archived:
            guarded_window = _validate_abort_window(
                roots,
                runtime_config,
                frozen_runtime_probe_snapshot=(
                    None if first_publication else window["runtime_probe_snapshot"]
                ),
            )
            if guarded_window != window:
                raise WorkflowError("abort session/action window changed during physical transaction")
        if first_publication:
            guarded_config = _abort_config_classification(roots, manifest, runtime_config)
            guarded_preflight = _abort_preflight_document(
                args,
                roots,
                manifest_hash,
                runtime_config,
                guarded_window,
                guarded_config,
                source_save,
                loader_save,
            )
            if guarded_preflight != preflight:
                raise WorkflowError("abort strict window changed at the first physical publication")
            first_publication = False

    def guarded_after_physical(label: str, target: pathlib.Path) -> None:
        nonlocal runtime_bridge_archived
        if transaction_after_physical_hook is not None:
            transaction_after_physical_hook(label, target)
        if label == "abort_archive_runtime_bridge":
            runtime_bridge_archived = True

    tx = FileTransaction(
        journal,
        abort_root,
        fault_after=int(getattr(args, "test_abort_fail_after_step", 0) or 0),
        after_step_hook=transaction_after_step_hook,
        after_physical_hook=guarded_after_physical,
        before_undo_claim_hook=transaction_before_undo_claim_hook,
        before_publish_hook=guarded_before_publish,
        journal_append=_append_abort_transaction_journal(plan),
    )
    result_intent: Optional[dict[str, Any]] = None
    try:
        _execute_abort_transaction_plan(tx, roots, plan)
        _abort_external_anchor_guard(args, roots, manifest, manifest_hash, runtime_config, plan)
        _verify_abort_restored_state(
            roots,
            manifest,
            source_save,
            loader_save,
            preflight,
            window,
            abort_root,
        )
        require_processes_stopped(process_counts(args, roots))
        result_intent = _publish_abort_result_intent(
            roots,
            manifest_hash,
            runtime_config,
            preflight_sha256,
            plan,
            config_state,
            window,
            manifest,
            args,
            loader_save,
        )
        # The durable intent is deliberately written before the commit record.
        # Rebuild every external and restored-state guard once more at that
        # exact boundary so a process/session/assembly race after the earlier
        # verification cannot be authenticated as a successful abort.
        if transaction_before_commit_hook is not None:
            transaction_before_commit_hook(plan)
        _abort_external_anchor_guard(args, roots, manifest, manifest_hash, runtime_config, plan)
        _verify_abort_restored_state(
            roots,
            manifest,
            source_save,
            loader_save,
            preflight,
            window,
            abort_root,
        )
        require_processes_stopped(process_counts(args, roots))
        tx.commit()
        if transaction_after_commit_hook is not None:
            transaction_after_commit_hook(plan)
    except Exception as transaction_error:
        if not tx.committed:
            try:
                tx.rollback()
                _normalize_abort_plan_initial_mtimes(
                    roots,
                    plan,
                    _append_abort_transaction_journal(plan),
                )
            except Exception as recovery_error:
                latch = abort_root / "transaction-recovery.json"
                if not os.path.lexists(latch):
                    _publish_abort_transaction_latch(
                        roots,
                        transaction_id=str(plan["transaction_id"]),
                        plan_hash=str(plan["plan_hash"]),
                        state="rollback_parent_mtime_outcome_unknown",
                        failures=[type(recovery_error).__name__],
                    )
                raise WorkflowError(
                    "abort rollback outcome_unknown; transaction latch retained"
                ) from recovery_error
        raise transaction_error

    if result_intent is None:
        raise WorkflowError("abort physical commit has no durable result intent")
    commit_entry = _abort_commit_entry(roots, plan, required=True)
    assert commit_entry is not None
    result = _abort_result_from_intent(result_intent, commit_entry)
    try:
        _abort_external_anchor_guard(args, roots, manifest, manifest_hash, runtime_config, plan)
        if contract_canonical_hash(_abort_final_state_document(args, roots, manifest, loader_save)) != result[
            "final_state_hash"
        ]:
            raise WorkflowError("abort final state changed after physical commit")
    except Exception as exc:
        publish_abort_recovery(
            roots,
            manifest_hash,
            runtime_config,
            preflight_sha256,
            plan,
            commit_entry,
            "abort_postcommit_guard_outcome_unknown",
            candidate_result=result,
            error_type=type(exc).__name__,
        )
        raise WorkflowError("abort postcommit guard failed; non-replay recovery published") from exc

    def abort_result_fault(_path: pathlib.Path) -> None:
        if bool(getattr(args, "test_abort_result_fail_after_verify", False)):
            raise WorkflowError("injected abort-result post-verification failure")

    result_path = abort_root / "abort-result.json"
    try:
        write_json_atomic(
            result_path,
            result,
            create_only=True,
            after_verify_hook=abort_result_fault,
        )
    except Exception as exc:
        publish_abort_recovery(
            roots,
            manifest_hash,
            runtime_config,
            preflight_sha256,
            plan,
            commit_entry,
            "abort_result_publication_outcome_unknown",
            candidate_result=result,
            error_type=type(exc).__name__,
        )
        raise WorkflowError(
            "abort result publication failed after physical commit; non-replay recovery published"
        ) from exc
    try:
        if bool(getattr(args, "test_abort_journal_fail_after_commit", False)):
            raise WorkflowError("injected abort journal acknowledgement failure")
        append_journal(
            journal,
            "environment_aborted",
            {"abort_result_sha256": sha256_file(result_path)},
        )
    except Exception as exc:
        publish_abort_recovery(
            roots,
            manifest_hash,
            runtime_config,
            preflight_sha256,
            plan,
            commit_entry,
            "abort_result_published_journal_outcome_unknown",
            candidate_result=result,
            error_type=type(exc).__name__,
        )
        raise WorkflowError("abort result is immutable but journal acknowledgement is outcome_unknown") from exc
    return result


def abort(args: argparse.Namespace) -> dict[str, Any]:
    roots = resolve_roots(args)
    reject_test_only_abort_options(args, test_root_present=roots.test_root is not None)
    if args.apply and args.confirm_live_run_id != args.run_id:
        raise WorkflowError("abort apply requires an exact --confirm-live-run-id")
    if not args.apply:
        return abort_locked(args, roots)
    with exclusive_finalize_lock(roots):
        return abort_locked(args, roots)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--oni-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--boundary-report-root", required=True)
    parser.add_argument("--pre-boundary", required=True)
    parser.add_argument("--clone-manifest", required=True)
    parser.add_argument("--source-save", required=True)
    parser.add_argument("--loader-save", required=True)
    parser.add_argument("--loader-stem", required=True)
    parser.add_argument("--test-root")
    parser.add_argument("--process-state-fixture")
    parser.add_argument("--confirm-live-run-id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--what-if", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="Bind and back up the immutable pre-run environment.")
    add_common(prepare_parser)
    prepare_parser.set_defaults(func=prepare)
    finalize_parser = subparsers.add_parser("finalize", help="Transactionally restore and post-compare the environment.")
    add_common(finalize_parser)
    finalize_parser.add_argument("--manifest", required=True)
    finalize_parser.add_argument("--lineage-manifest", required=True)
    finalize_parser.add_argument("--priority-pair-report", required=True)
    finalize_parser.add_argument("--priority-pair-report-hash", required=True)
    finalize_parser.add_argument("--shutdown-evidence", required=True)
    finalize_parser.add_argument("--shutdown-evidence-hash", required=True)
    finalize_parser.add_argument("--startup-consumption-bundle", required=True)
    finalize_parser.add_argument("--startup-consumption-evidence-hash", required=True)
    finalize_parser.add_argument("--validation-process-id", type=int, required=True)
    finalize_parser.add_argument("--runtime-config-evidence", required=True)
    finalize_parser.add_argument("--expected-runtime-config-evidence-sha256", required=True)
    finalize_parser.add_argument("--expected-startup-binding-sha256", required=True)
    finalize_parser.add_argument("--runtime-bootstrap-result", required=True)
    finalize_parser.add_argument(
        "--expected-runtime-bootstrap-result-sha256",
        required=True,
    )
    finalize_parser.add_argument("--release-dll", required=True)
    finalize_parser.add_argument("--expected-release-dll-sha256", required=True)
    finalize_parser.add_argument("--game-managed-dir", required=True)
    finalize_parser.add_argument(
        "--test-managed-assembly-csharp-sha256",
        help=argparse.SUPPRESS,
    )
    finalize_parser.add_argument(
        "--test-managed-assembly-csharp-firstpass-sha256",
        help=argparse.SUPPRESS,
    )
    finalize_parser.add_argument("--forward-step-id", required=True)
    finalize_parser.add_argument("--inverse-step-id", required=True)
    finalize_parser.add_argument("--command-id", action="append", required=True)
    finalize_parser.add_argument("--command-bundle-hash", action="append", required=True)
    finalize_parser.add_argument("--test-fail-after-step", type=int, default=0, help=argparse.SUPPRESS)
    finalize_parser.add_argument(
        "--test-finalize-result-fail-after-verify",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    finalize_parser.set_defaults(func=finalize)
    abort_parser = subparsers.add_parser(
        "abort",
        help="Transactionally restore a prepared/configured run before any session or action evidence exists.",
    )
    add_common(abort_parser)
    abort_parser.add_argument("--manifest", required=True)
    abort_parser.add_argument("--expected-environment-manifest-sha256", required=True)
    abort_parser.add_argument("--runtime-config-evidence", required=True)
    abort_parser.add_argument("--expected-runtime-config-evidence-sha256", required=True)
    abort_parser.add_argument("--expected-startup-binding-sha256", required=True)
    abort_parser.add_argument("--runtime-bootstrap-result", required=True)
    abort_parser.add_argument(
        "--expected-runtime-bootstrap-result-sha256",
        required=True,
    )
    abort_parser.add_argument("--release-dll", required=True)
    abort_parser.add_argument("--expected-release-dll-sha256", required=True)
    abort_parser.add_argument("--game-managed-dir", required=True)
    abort_parser.add_argument(
        "--test-managed-assembly-csharp-sha256",
        help=argparse.SUPPRESS,
    )
    abort_parser.add_argument(
        "--test-managed-assembly-csharp-firstpass-sha256",
        help=argparse.SUPPRESS,
    )
    abort_parser.add_argument("--test-abort-fail-after-step", type=int, default=0, help=argparse.SUPPRESS)
    abort_parser.add_argument(
        "--test-abort-result-fail-after-verify",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    abort_parser.add_argument(
        "--test-abort-journal-fail-after-commit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    abort_parser.set_defaults(func=abort)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "finalize":
            reject_test_only_finalize_options(
                args,
                test_root_present=bool(args.test_root),
            )
        elif args.command == "abort":
            reject_test_only_abort_options(
                args,
                test_root_present=bool(args.test_root),
            )
        result = args.func(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except PostValidationError as exc:
        print(
            json.dumps(
                {
                    "schema": "oni.live-validation-workflow-error.v1",
                    "status": "blocked",
                    "classification": "behavior_fail",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    except WorkflowError as exc:
        print(
            json.dumps(
                {
                    "schema": "oni.live-validation-workflow-error.v1",
                    "status": "blocked",
                    "classification": "infra_fail",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Raw OS exceptions can contain full user paths.  Keep unexpected
        # implementation/I/O failures private while still failing closed.
        print(
            json.dumps(
                {
                    "schema": "oni.live-validation-workflow-error.v1",
                    "status": "blocked",
                    "classification": "infra_fail",
                    "error": "unexpected local I/O or data-shape failure",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
