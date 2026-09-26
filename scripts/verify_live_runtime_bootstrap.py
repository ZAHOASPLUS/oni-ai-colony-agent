#!/usr/bin/env python3
"""Offline adversarial verification for manage_live_runtime_bootstrap.py.

Every fixture is rooted in a fresh TestRoot.  The verifier never discovers or
writes the real Klei directory, never builds the mod, and never launches ONI.
Two complete rounds run by default so recovery/replay acceptance is not based
on a single lucky filesystem schedule.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANAGER = PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py"
REPORT = PROJECT_ROOT / "reports" / "live-runtime-bootstrap-verification.json"
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import manage_live_runtime_bootstrap as core


MAX_FIXTURE_BASENAME_UTF16_UNITS = 20
MAX_FIXTURE_RUN_ID_UTF16_UNITS = 24
ABORT_RELOCATED_SOURCE_MARKER = "dup.bin"


def _maximum_path_budget_abort_contract(
    roots: core.Roots,
    archive_name: str,
) -> tuple[dict[str, Any], dict[str, pathlib.Path]]:
    """Return the maximal ordinary abort shape used by path-budget probes."""

    steps = [
        ("runtime_bridge_archived", "move"),
        ("runtime_probe_archived", "move"),
        ("created_archive_root_archived", "move"),
        ("old_bridge_restored", "move"),
        ("old_probe_restored", "move"),
        ("archive_root_mtime_restored", "set_mtime"),
        ("probe_parent_mtime_restored", "set_mtime"),
        ("probe_container_mtime_restored", "set_mtime"),
        ("oni_root_mtime_restored", "set_mtime"),
    ]
    abort_plan = {
        "archive_name": archive_name,
        "steps": [
            {"index": index, "event": event, "operation": operation}
            for index, (event, operation) in enumerate(steps)
        ],
    }
    save_parent = roots.oni_root / "save_files" / "FixtureColony"
    source_save = save_parent / "Original.sav"
    loader_save = save_parent / f"{roots.run_id}-loader.sav"
    explicit = {
        "pre_boundary": (
            roots.evidence_root
            / "live-boundaries"
            / roots.run_id
            / "pre-boundary.json"
        ),
        "clone_manifest": (
            roots.evidence_root
            / "runtime"
            / roots.run_id
            / "test-clone-manifest.json"
        ),
        "source_save": source_save,
        "source_preview": source_save.with_suffix(".png"),
        "loader_save": loader_save,
        "loader_preview": loader_save.with_suffix(".png"),
    }
    return abort_plan, explicit


INSTALL_RESULT_KEYS = frozenset(
    {
        "schema",
        "status",
        "classification",
        "run_id",
        "transaction_id",
        "plan_hash",
        "environment_manifest_sha256",
        "release_dll_sha256",
        "expected_mod_version",
        "probe_tree_content_hash",
        "installed_probe_artifacts",
        "installed_probe_tree_identity",
        "bridge_tree_content_hash",
        "bootstrap_document_hash",
        "bootstrap_file_sha256",
        "archive_relative",
        "old_probe_recovery_relative",
        "install_commit_entry",
        "install_commit_entry_hash",
        "install_stage_sha256",
        "install_stage_hash",
        "producer",
        "install_lock_epoch",
        "install_lock_claim",
        "install_lock_claim_hash",
        "install_lock_release",
        "install_lock_release_hash",
        "ready_lock_epoch",
        "ready_lock_claim",
        "ready_lock_claim_hash",
        "ready_lock_release",
        "ready_lock_release_hash",
        "candidate_sha256",
        "candidate_hash",
        "physical_commit_complete",
        "shared_lock_released",
        "game_launched",
        "result_hash",
    }
)


STAGE_FAULT_POINTS = (
    "after_install_stage_publish",
)


ABORT_FAULT_POINTS = (
    "after_abort_owner_publish",
    "after_abort_intent_publish",
    "after_abort_plan_publish",
    "after_abort_journal_initialized",
    "after_environment_lock_attempt_parent_claim",
    "after_environment_lock_attempt_dir",
    "after_environment_lock_intent",
    "after_environment_lock_stage_preclaim",
    "after_environment_lock_stage_fsync",
    "after_environment_lock_stage_ready",
    "before_environment_lock_publish",
    "after_environment_lock_publish",
    "after_environment_lock_acquired",
    "before_abort_runtime_bridge_archived_physical",
    "after_abort_runtime_bridge_archived_physical",
    "after_abort_runtime_bridge_archived_journal",
    "before_abort_runtime_probe_archived_physical",
    "after_abort_runtime_probe_archived_physical",
    "after_abort_runtime_probe_archived_journal",
    "before_abort_created_archive_root_archived_physical",
    "after_abort_created_archive_root_archived_physical",
    "after_abort_created_archive_root_archived_journal",
    "before_abort_old_bridge_restored_physical",
    "after_abort_old_bridge_restored_physical",
    "after_abort_old_bridge_restored_journal",
    "before_abort_old_probe_restored_physical",
    "after_abort_old_probe_restored_physical",
    "after_abort_old_probe_restored_journal",
    "after_abort_probe_parent_mtime_restored_claim_prepared_journal",
    "before_abort_probe_parent_mtime_restored_physical",
    "after_abort_probe_parent_mtime_restored_handle_open",
    "after_abort_probe_parent_mtime_restored_handle_settime",
    "after_abort_probe_parent_mtime_restored_physical",
    "after_abort_probe_parent_mtime_restored_journal",
    "after_abort_probe_container_mtime_restored_claim_prepared_journal",
    "before_abort_probe_container_mtime_restored_physical",
    "after_abort_probe_container_mtime_restored_handle_open",
    "after_abort_probe_container_mtime_restored_handle_settime",
    "after_abort_probe_container_mtime_restored_physical",
    "after_abort_probe_container_mtime_restored_journal",
    "after_abort_oni_root_mtime_restored_claim_prepared_journal",
    "before_abort_oni_root_mtime_restored_physical",
    "after_abort_oni_root_mtime_restored_handle_open",
    "after_abort_oni_root_mtime_restored_handle_settime",
    "after_abort_oni_root_mtime_restored_physical",
    "after_abort_oni_root_mtime_restored_journal",
    "after_abort_commit_verified_journal",
    "after_abort_result_candidate_publish",
    "before_environment_lock_release",
    "after_environment_lock_release_physical",
    "after_environment_lock_released",
    "after_abort_physical_lock_release",
    "after_abort_ready_result_publish",
    "before_abort_ready_lock_release",
    "after_abort_ready_lock_release",
)


ABORT_ARCHIVE_MTIME_FAULT_POINTS = (
    "after_abort_archive_root_mtime_restored_claim_prepared_journal",
    "before_abort_archive_root_mtime_restored_physical",
    "after_abort_archive_root_mtime_restored_handle_open",
    "after_abort_archive_root_mtime_restored_handle_settime",
    "after_abort_archive_root_mtime_restored_physical",
    "after_abort_archive_root_mtime_restored_journal",
)


ABORT_READY_LOCK_SECOND_OCCURRENCE_FAULT_POINTS = (
    "after_environment_lock_attempt_parent_claim",
    "after_environment_lock_stage_preclaim",
    "after_environment_lock_stage_fsync",
    "after_environment_lock_publish",
    "after_environment_lock_acquired",
    "after_environment_lock_release_physical",
    "after_environment_lock_released",
)


INITIALIZATION_CLAIM_POINTS = (
    "after_transaction_root_claim",
    "after_transaction_lock_claim",
    "after_stage_root_claim",
    "after_probe_stage_root_claim",
    "after_bridge_stage_root_claim",
    "after_archive_stage_root_claim",
    "after_recovery_root_claim",
    "after_lock_attempt_root_claim",
    "after_journal_root_claim",
    "after_abort_support_root_claim",
    "after_abort_custody_root_claim",
    "after_abort_journal_root_claim",
)


INITIALIZATION_PRECLAIM_FAULT_POINTS = tuple(
    point + "_physical_before_claim" for point in INITIALIZATION_CLAIM_POINTS
)


FAULT_POINTS = (
    *INITIALIZATION_CLAIM_POINTS,
    "after_owner_publish",
    "after_intent_publish",
    "after_plan_publish",
    "after_journal_initialized",
    "after_probe_stage_physical",
    "after_probe_stage_journal",
    "after_bridge_stage_physical",
    "after_bridge_stage_journal",
    "after_archive_root_stage_physical",
    "after_archive_root_stage_journal",
    "after_archive_root_physical",
    "after_archive_root_journal",
    "after_old_bridge_physical",
    "after_old_bridge_journal",
    "after_old_probe_physical",
    "after_old_probe_journal",
    "after_new_probe_physical",
    "after_new_probe_journal",
    "after_new_bridge_physical",
    "after_new_bridge_journal",
    "after_environment_lock_attempt_dir",
    "after_environment_lock_intent",
    "after_environment_lock_stage_preclaim",
    "after_environment_lock_stage_fsync",
    "after_environment_lock_stage_ready",
    "before_environment_lock_publish",
    "after_environment_lock_publish",
    "after_environment_lock_acquired",
    "after_commit_verified_journal",
    "after_result_candidate_publish",
    "before_environment_lock_release",
    "after_environment_lock_release_physical",
    "after_environment_lock_released",
    "after_environment_lock_release",
    "after_ready_result_publish",
    "before_ready_publication_lock_release",
    "after_ready_publication_lock_release",
)


@dataclass
class Fixture:
    test_root: pathlib.Path
    oni: pathlib.Path
    evidence: pathlib.Path
    release: pathlib.Path
    run_id: str
    manifest: pathlib.Path
    manifest_hash: str
    release_hash: str
    process_fixture: pathlib.Path
    pre_boundary: pathlib.Path
    clone_manifest: pathlib.Path
    source_save: pathlib.Path
    loader_save: pathlib.Path
    loader_stem: str
    args: list[str]
    stage_nonce: str
    expected_stage_sha256: Optional[str] = None
    expected_transaction_id: Optional[str] = None
    abort_stage_nonce: str = ""
    install_result_sha256: Optional[str] = None
    expected_abort_stage_sha256: Optional[str] = None
    expected_abort_id: Optional[str] = None


def write_json(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(core.document_bytes(value))


def create_environment_journal(
    path: pathlib.Path,
    manifest_hash: str,
    *,
    pre_boundary_hash: str = "sha256:" + "1" * 64,
    clone_manifest_hash: str = "sha256:" + "2" * 64,
) -> None:
    entry: dict[str, Any] = {
        "schema": core.ENVIRONMENT_JOURNAL_SCHEMA,
        "index": 1,
        "recorded_at": "2026-08-31T00:00:00+00:00",
        "event": "environment_prepared",
        "data": {
            "environment_manifest_sha256": manifest_hash,
            "pre_boundary_sha256": pre_boundary_hash,
            "clone_manifest_sha256": clone_manifest_hash,
        },
        "previous_entry_hash": None,
    }
    entry["entry_hash"] = core.canonical_hash(entry)
    path.write_bytes(core.canonical_bytes(entry) + b"\n")


def fixture_basename(name: str) -> str:
    """Return a stable fixture leaf that preserves the production path budget."""

    if not isinstance(name, str) or not name:
        raise AssertionError("fixture name must be a non-empty string")
    if len(name.encode("utf-16-le")) // 2 <= MAX_FIXTURE_BASENAME_UTF16_UNITS:
        return name

    token = core.sha256_bytes(name.encode("utf-8")).removeprefix("sha256:")[:10]
    prefix = ""
    for character in name:
        candidate = prefix + character
        if len(candidate.encode("utf-16-le")) // 2 > 9:
            break
        prefix = candidate
    compacted = f"{prefix}-{token}"
    if len(compacted.encode("utf-16-le")) // 2 > MAX_FIXTURE_BASENAME_UTF16_UNITS:
        raise AssertionError("compacted fixture basename exceeds its UTF-16 budget")
    return compacted


def _ordinary_fixture_path_budget_candidates(
    base: pathlib.Path,
) -> list[pathlib.Path]:
    """Return production and verifier-only paths used by an ordinary fixture."""

    fixture_name = "f" * MAX_FIXTURE_BASENAME_UTF16_UNITS
    run_id = f"run-{fixture_name}"
    archive_name = f"{run_id}-preexisting-rtb-0123456789abcdef01234567"
    fixture_root = base / "b000" / fixture_name
    roots = core.Roots(
        run_id,
        fixture_root / "oni",
        fixture_root / "evidence",
        fixture_root,
    )
    plan = {
        "desired": {
            "bootstrap": {"bridge_dir": str(roots.oni_root / "ai-bridge")},
            "archive_relative": f"ai-bridge-archives/{archive_name}",
        },
        "prestate": {
            "old_bridge": {
                "snapshot": {"entries": [{"relative": "old-state.json"}]}
            },
            "probe": {"snapshot": {"entries": [{"relative": "legacy.dll"}]}},
        },
    }
    abort_plan, explicit = _maximum_path_budget_abort_contract(
        roots,
        archive_name,
    )
    candidates = list(
        core.legacy_path_candidates(
            roots,
            plan,
            abort_plan,
            explicit,
        )
    )
    roles = core._abort_role_paths(roots, abort_plan, explicit)
    relocated_source = roles.get("relocated_old_bridge_archive")
    if not isinstance(relocated_source, pathlib.Path):
        raise AssertionError("ordinary fixture lacks relocated old-Bridge path")
    candidates.append(relocated_source / ABORT_RELOCATED_SOURCE_MARKER)
    return candidates


def assert_actual_fixture_path_budget(base: pathlib.Path) -> None:
    """Fail before round work if any maximum-size ordinary fixture cannot fit."""

    candidates = _ordinary_fixture_path_budget_candidates(base)
    maximum_units, maximum_path = max(
        (core.utf16_units(path), path) for path in candidates
    )
    if maximum_units > 259:
        raise AssertionError(
            "ordinary fixture path preflight exceeds the 259 UTF-16-unit "
            f"legacy budget: units={maximum_units} path={maximum_path}"
        )


def _batch_root_name(index: int) -> str:
    return f"b{index:03d}"


def _snapshot_direct_children(target: pathlib.Path) -> dict[str, Any]:
    if not target.exists():
        raise AssertionError(f"batch child snapshot target does not exist: {target}")
    metadata = os.lstat(target)
    if core.is_reparse_metadata(metadata) or not target.is_dir():
        raise AssertionError(f"batch root is unsafe: {target}")
    entries: dict[str, Any] = {}
    for entry in sorted(target.iterdir()):
        metadata = os.lstat(entry)
        if core.is_reparse_metadata(metadata):
            raise AssertionError(f"batch root contains reparse child: {entry}")
        entries[entry.name] = core.node_identity(metadata)
    return entries


def _list_created_batch_children(
    batch_root: pathlib.Path,
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[pathlib.Path]:
    for name, identity in before.items():
        current = batch_root / name
        if not current.exists():
            raise AssertionError(
                f"batch root pre-existing child was removed: {current}"
            )
        if core.node_identity(os.lstat(current)) != identity:
            raise AssertionError(
                f"batch root child identity drifted: {current}"
            )

    return [
        batch_root / name
        for name in sorted(after)
        if name not in before
    ]


def _assert_removable_batch_fixture(root: pathlib.Path, batch_root: pathlib.Path) -> None:
    if root.parent != batch_root:
        raise AssertionError(
            f"batch fixture escaped its parent directory: path={root}"
        )
    metadata = os.lstat(root)
    if core.is_reparse_metadata(metadata) or not root.is_dir():
        raise AssertionError(f"batch fixture is not a regular directory: {root}")
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in [*dirnames, *filenames]:
            child = pathlib.Path(dirpath) / name
            child_metadata = os.lstat(child)
            if core.is_reparse_metadata(child_metadata):
                raise AssertionError(f"batch fixture contains reparse node: {child}")


@dataclass(frozen=True)
class _CleanupNode:
    relative: str
    kind: str
    identity: tuple[int, int, int]
    parent_identity: tuple[int, int, int]
    depth: int


@dataclass(frozen=True)
class _FixtureHardlinkAliasToken:
    fixture_root: pathlib.Path
    fixture_identity: tuple[int, int, int]
    fixture_parent_identity: tuple[int, int, int]
    parent: pathlib.Path
    parent_identity: tuple[int, int, int]
    original: pathlib.Path
    alias: pathlib.Path
    file_identity: tuple[int, int, int]
    size: int
    mtime_ns: int
    sha256: str


_CLEANUP_TEST_HOOK: Optional[Callable[[str, pathlib.Path], None]] = None


def _cleanup_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return tuple(core.node_identity(metadata))  # type: ignore[return-value]


def _cleanup_kind(metadata: os.stat_result, path: pathlib.Path) -> str:
    if core.is_reparse_metadata(metadata):
        raise AssertionError(f"cleanup tree contains reparse node: {path}")
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        if int(metadata.st_nlink) != 1:
            raise AssertionError(f"cleanup tree contains hard-linked file: {path}")
        return "file"
    raise AssertionError(f"cleanup tree contains non-ordinary node: {path}")


def _cleanup_lstat(
    path: pathlib.Path,
) -> tuple[os.stat_result, str, tuple[int, int, int]]:
    if not os.path.lexists(path):
        raise AssertionError(f"cleanup node is missing: {path}")
    metadata = os.lstat(path)
    return metadata, _cleanup_kind(metadata, path), _cleanup_identity(metadata)


def _capture_cleanup_tree(batch_root: pathlib.Path) -> tuple[_CleanupNode, ...]:
    """Capture all cleanup nodes before the first destructive operation."""

    root_metadata, root_kind, root_identity = _cleanup_lstat(batch_root)
    del root_metadata
    if root_kind != "directory":
        raise AssertionError(f"cleanup root is not a regular directory: {batch_root}")
    parent_metadata, parent_kind, parent_identity = _cleanup_lstat(batch_root.parent)
    del parent_metadata
    if parent_kind != "directory":
        raise AssertionError(f"cleanup parent is not a regular directory: {batch_root.parent}")
    records: dict[str, _CleanupNode] = {}

    def visit(
        path: pathlib.Path,
        relative: str,
        expected_parent_identity: tuple[int, int, int],
        depth: int,
    ) -> None:
        metadata, kind, identity = _cleanup_lstat(path)
        del metadata
        current_parent = _cleanup_lstat(path.parent)
        if current_parent[1] != "directory" or current_parent[2] != expected_parent_identity:
            raise AssertionError(f"cleanup node parent identity changed: {path}")
        records[relative] = _CleanupNode(
            relative,
            kind,
            identity,
            expected_parent_identity,
            depth,
        )
        if kind != "directory":
            return
        with os.scandir(path) as iterator:
            names = sorted(entry.name for entry in iterator)
        for name in names:
            child_relative = name if relative == "." else f"{relative}/{name}"
            visit(path / name, child_relative, identity, depth + 1)
        after = _cleanup_lstat(path)
        if after[1] != "directory" or after[2] != identity:
            raise AssertionError(f"cleanup directory identity changed: {path}")
        with os.scandir(path) as iterator:
            names_after = sorted(entry.name for entry in iterator)
        if names_after != names:
            raise AssertionError(f"cleanup directory entries changed: {path}")

    visit(batch_root, ".", parent_identity, 0)
    if records["."].identity != root_identity:
        raise AssertionError(f"cleanup root identity changed: {batch_root}")
    return tuple(records[name] for name in sorted(records, key=str.casefold))


def _cleanup_path(batch_root: pathlib.Path, node: _CleanupNode) -> pathlib.Path:
    if node.relative == ".":
        return batch_root
    return batch_root / pathlib.Path(*pathlib.PurePosixPath(node.relative).parts)


def _assert_cleanup_name(
    path: pathlib.Path,
    node: _CleanupNode,
) -> None:
    metadata, kind, identity = _cleanup_lstat(path)
    del metadata
    if kind != node.kind or identity != node.identity:
        raise AssertionError(f"cleanup node identity changed: {path}")
    parent = _cleanup_lstat(path.parent)
    if parent[1] != "directory" or parent[2] != node.parent_identity:
        raise AssertionError(f"cleanup node parent identity changed: {path}")


def _windows_kernel32() -> Any:
    import ctypes.wintypes as wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _open_cleanup_delete_descriptor(path: pathlib.Path) -> int:
    import msvcrt

    kernel32 = _windows_kernel32()
    raw_handle = kernel32.CreateFileW(
        str(path),
        0x0080 | 0x00010000,  # FILE_READ_ATTRIBUTES | DELETE
        0x0001 | 0x0002,  # share read/write; pin the node against rename/delete
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle in (None, invalid_handle):
        raise OSError(
            ctypes.get_last_error(),
            "CreateFileW could not bind cleanup node",
            str(path),
        )
    try:
        return msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(raw_handle)
        raise


def _open_cleanup_custody_descriptor(path: pathlib.Path) -> int:
    """Open a directory without FILE_SHARE_DELETE to pin its name in place."""

    import msvcrt

    kernel32 = _windows_kernel32()
    raw_handle = kernel32.CreateFileW(
        str(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x0001 | 0x0002,  # share read/write, deliberately not delete
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle in (None, invalid_handle):
        raise OSError(
            ctypes.get_last_error(),
            "CreateFileW could not acquire cleanup directory custody",
            str(path),
        )
    try:
        return msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(raw_handle)
        raise


@dataclass
class _CleanupDirectoryCustody:
    path: pathlib.Path
    identity: tuple[int, int, int]
    parent_identity: tuple[int, int, int]
    descriptor: Optional[int]

    def validate(self) -> None:
        metadata, kind, identity = _cleanup_lstat(self.path)
        del metadata
        if kind != "directory" or identity != self.identity:
            raise AssertionError(f"cleanup custody identity changed: {self.path}")
        parent = _cleanup_lstat(self.path.parent)
        if parent[1] != "directory" or parent[2] != self.parent_identity:
            raise AssertionError(f"cleanup custody parent changed: {self.path.parent}")
        if self.descriptor is not None:
            opened = os.fstat(self.descriptor)
            if (
                _cleanup_kind(opened, self.path) != "directory"
                or _cleanup_identity(opened) != self.identity
            ):
                raise AssertionError(f"cleanup custody handle changed: {self.path}")

    def close(self) -> None:
        if self.descriptor is not None:
            descriptor = self.descriptor
            self.descriptor = None
            os.close(descriptor)


def _acquire_cleanup_directory_custody(
    path: pathlib.Path,
    expected_identity: Optional[Sequence[int]] = None,
) -> _CleanupDirectoryCustody:
    metadata, kind, identity = _cleanup_lstat(path)
    del metadata
    if kind != "directory":
        raise AssertionError(f"cleanup custody target is not a directory: {path}")
    if expected_identity is not None and identity != tuple(expected_identity):
        raise AssertionError(f"cleanup custody identity changed before open: {path}")
    parent = _cleanup_lstat(path.parent)
    if parent[1] != "directory":
        raise AssertionError(f"cleanup custody parent is unsafe: {path.parent}")
    descriptor = (
        _open_cleanup_custody_descriptor(path)
        if os.name == "nt"
        else None
    )
    custody = _CleanupDirectoryCustody(path, identity, parent[2], descriptor)
    try:
        custody.validate()
    except Exception:
        custody.close()
        raise
    return custody


def _set_cleanup_delete_disposition(descriptor: int, path: pathlib.Path) -> None:
    import ctypes.wintypes as wintypes
    import msvcrt

    class DispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    class DispositionInfoEx(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD)]

    kernel32 = _windows_kernel32()
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    extended = DispositionInfoEx(0x0001 | 0x0002 | 0x0010)
    if kernel32.SetFileInformationByHandle(
        handle,
        21,  # FileDispositionInfoEx
        ctypes.byref(extended),
        ctypes.sizeof(extended),
    ):
        return
    extended_error = ctypes.get_last_error()
    basic = DispositionInfo(True)
    if kernel32.SetFileInformationByHandle(
        handle,
        4,  # FileDispositionInfo
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):
        return
    raise OSError(
        ctypes.get_last_error(),
        f"SetFileInformationByHandle failed (extended={extended_error})",
        str(path),
    )


def _fixture_hardlink_regular_metadata(
    path: pathlib.Path,
    label: str,
) -> os.stat_result:
    if not os.path.lexists(path):
        raise AssertionError(f"{label} is missing: {path}")
    metadata = os.lstat(path)
    if core.is_reparse_metadata(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise AssertionError(f"{label} is not a regular non-reparse file: {path}")
    return metadata


def _fixture_hardlink_directory_metadata(
    path: pathlib.Path,
    label: str,
) -> os.stat_result:
    if not os.path.lexists(path):
        raise AssertionError(f"{label} is missing: {path}")
    metadata = os.lstat(path)
    if core.is_reparse_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise AssertionError(f"{label} is not a regular non-reparse directory: {path}")
    return metadata


def _assert_fixture_hardlink_alias(
    token: _FixtureHardlinkAliasToken,
    expected_nlink: int,
) -> None:
    fixture_metadata = _fixture_hardlink_directory_metadata(
        token.fixture_root,
        "fixture hardlink root",
    )
    fixture_parent_metadata = _fixture_hardlink_directory_metadata(
        token.fixture_root.parent,
        "fixture hardlink root parent",
    )
    parent_metadata = _fixture_hardlink_directory_metadata(
        token.parent,
        "fixture hardlink parent",
    )
    if _cleanup_identity(fixture_metadata) != token.fixture_identity:
        raise AssertionError("fixture hardlink root identity changed")
    if _cleanup_identity(fixture_parent_metadata) != token.fixture_parent_identity:
        raise AssertionError("fixture hardlink root parent identity changed")
    if _cleanup_identity(parent_metadata) != token.parent_identity:
        raise AssertionError("fixture hardlink parent identity changed")
    if (
        token.original.parent != token.parent
        or token.alias.parent != token.parent
        or not core.path_within(token.parent, token.fixture_root)
        or not core.path_within(token.original, token.fixture_root)
        or not core.path_within(token.alias, token.fixture_root)
    ):
        raise AssertionError("fixture hardlink paths escaped their recorded sibling parent")

    original_metadata = _fixture_hardlink_regular_metadata(
        token.original,
        "fixture hardlink original",
    )
    alias_metadata = _fixture_hardlink_regular_metadata(
        token.alias,
        "fixture hardlink alias",
    )
    if (
        _cleanup_identity(original_metadata) != token.file_identity
        or _cleanup_identity(alias_metadata) != token.file_identity
    ):
        raise AssertionError("fixture hardlink inode identity changed")
    if (
        int(original_metadata.st_nlink) != expected_nlink
        or int(alias_metadata.st_nlink) != expected_nlink
    ):
        raise AssertionError(
            "fixture hardlink link count changed: "
            f"expected={expected_nlink} "
            f"original={int(original_metadata.st_nlink)} "
            f"alias={int(alias_metadata.st_nlink)}"
        )
    if (
        int(original_metadata.st_size) != token.size
        or int(alias_metadata.st_size) != token.size
        or int(original_metadata.st_mtime_ns) != token.mtime_ns
        or int(alias_metadata.st_mtime_ns) != token.mtime_ns
        or core.sha256_bytes(token.original.read_bytes()) != token.sha256
    ):
        raise AssertionError("fixture hardlink content state changed")


def _create_fixture_hardlink_alias(
    fixture_root: pathlib.Path,
    original: pathlib.Path,
    alias: pathlib.Path,
) -> _FixtureHardlinkAliasToken:
    fixture_root = core.normalized(fixture_root)
    original = core.normalized(original)
    alias = core.normalized(alias)
    if original == alias or original.parent != alias.parent:
        raise AssertionError("fixture hardlink alias must be a distinct sibling")
    if (
        not core.path_within(original, fixture_root)
        or not core.path_within(alias, fixture_root)
        or not core.path_within(original.parent, fixture_root)
    ):
        raise AssertionError("fixture hardlink alias escaped its fixture root")
    if os.path.lexists(alias):
        raise AssertionError(f"fixture hardlink alias already exists: {alias}")

    fixture_metadata = _fixture_hardlink_directory_metadata(
        fixture_root,
        "fixture hardlink root",
    )
    fixture_parent_metadata = _fixture_hardlink_directory_metadata(
        fixture_root.parent,
        "fixture hardlink root parent",
    )
    parent_metadata = _fixture_hardlink_directory_metadata(
        original.parent,
        "fixture hardlink parent",
    )
    original_metadata = _fixture_hardlink_regular_metadata(
        original,
        "fixture hardlink original",
    )
    if int(original_metadata.st_nlink) != 1:
        raise AssertionError(
            "fixture hardlink original must have exactly one link before alias creation"
        )
    token = _FixtureHardlinkAliasToken(
        fixture_root=fixture_root,
        fixture_identity=_cleanup_identity(fixture_metadata),
        fixture_parent_identity=_cleanup_identity(fixture_parent_metadata),
        parent=original.parent,
        parent_identity=_cleanup_identity(parent_metadata),
        original=original,
        alias=alias,
        file_identity=_cleanup_identity(original_metadata),
        size=int(original_metadata.st_size),
        mtime_ns=int(original_metadata.st_mtime_ns),
        sha256=core.sha256_bytes(original.read_bytes()),
    )
    original_after_read = _fixture_hardlink_regular_metadata(
        original,
        "fixture hardlink original",
    )
    if (
        _cleanup_identity(original_after_read) != token.file_identity
        or int(original_after_read.st_nlink) != 1
        or int(original_after_read.st_size) != token.size
        or int(original_after_read.st_mtime_ns) != token.mtime_ns
    ):
        raise AssertionError("fixture hardlink original changed during capture")

    os.link(original, alias)
    _assert_fixture_hardlink_alias(token, 2)
    return token


def _release_fixture_hardlink_alias(token: _FixtureHardlinkAliasToken) -> None:
    """Remove only a provenance-bound fixture alias after its scenario passed."""

    _assert_fixture_hardlink_alias(token, 2)
    if os.name != "nt":
        raise AssertionError("fixture hardlink alias release requires Windows")

    root_custody: Optional[_CleanupDirectoryCustody] = None
    parent_custody: Optional[_CleanupDirectoryCustody] = None
    descriptor: Optional[int] = None
    try:
        root_custody = _acquire_cleanup_directory_custody(
            token.fixture_root,
            token.fixture_identity,
        )
        if root_custody.parent_identity != token.fixture_parent_identity:
            raise AssertionError("fixture hardlink root parent changed before alias release")
        parent_custody = _acquire_cleanup_directory_custody(
            token.parent,
            token.parent_identity,
        )
        _assert_fixture_hardlink_alias(token, 2)

        descriptor = _open_cleanup_delete_descriptor(token.alias)
        opened = os.fstat(descriptor)
        if (
            core.is_reparse_metadata(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _cleanup_identity(opened) != token.file_identity
            or int(opened.st_nlink) != 2
            or int(opened.st_size) != token.size
            or int(opened.st_mtime_ns) != token.mtime_ns
        ):
            raise AssertionError("fixture hardlink alias handle identity changed")
        _assert_fixture_hardlink_alias(token, 2)
        opened = os.fstat(descriptor)
        if (
            _cleanup_identity(opened) != token.file_identity
            or int(opened.st_nlink) != 2
        ):
            raise AssertionError("fixture hardlink alias changed before disposition")
        _set_cleanup_delete_disposition(descriptor, token.alias)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_custody is not None:
            parent_custody.close()
        if root_custody is not None:
            root_custody.close()

    if os.path.lexists(token.alias):
        raise AssertionError("fixture hardlink alias remained after disposition")
    fixture_metadata = _fixture_hardlink_directory_metadata(
        token.fixture_root,
        "fixture hardlink root",
    )
    fixture_parent_metadata = _fixture_hardlink_directory_metadata(
        token.fixture_root.parent,
        "fixture hardlink root parent",
    )
    parent_metadata = _fixture_hardlink_directory_metadata(
        token.parent,
        "fixture hardlink parent",
    )
    original_metadata = _fixture_hardlink_regular_metadata(
        token.original,
        "fixture hardlink original",
    )
    if (
        _cleanup_identity(fixture_metadata) != token.fixture_identity
        or _cleanup_identity(fixture_parent_metadata) != token.fixture_parent_identity
        or _cleanup_identity(parent_metadata) != token.parent_identity
        or _cleanup_identity(original_metadata) != token.file_identity
        or int(original_metadata.st_nlink) != 1
        or int(original_metadata.st_size) != token.size
        or int(original_metadata.st_mtime_ns) != token.mtime_ns
        or core.sha256_bytes(token.original.read_bytes()) != token.sha256
    ):
        raise AssertionError("fixture hardlink original changed after alias release")


def _delete_cleanup_node_windows(batch_root: pathlib.Path, node: _CleanupNode) -> None:
    path = _cleanup_path(batch_root, node)
    _assert_cleanup_name(path, node)
    if node.kind == "directory":
        with os.scandir(path) as iterator:
            residue = sorted(entry.name for entry in iterator)
        if residue:
            raise AssertionError(f"cleanup directory retained unexpected children: {path}")
    descriptor = _open_cleanup_delete_descriptor(path)
    try:
        opened = os.fstat(descriptor)
        if _cleanup_kind(opened, path) != node.kind or _cleanup_identity(opened) != node.identity:
            raise AssertionError(f"cleanup handle identity changed: {path}")
        _assert_cleanup_name(path, node)
        if _CLEANUP_TEST_HOOK is not None:
            _CLEANUP_TEST_HOOK("after_delete_handle_open", path)
        opened = os.fstat(descriptor)
        if _cleanup_kind(opened, path) != node.kind or _cleanup_identity(opened) != node.identity:
            raise AssertionError(f"cleanup handle identity changed after hook: {path}")
        _assert_cleanup_name(path, node)
        _set_cleanup_delete_disposition(descriptor, path)
    finally:
        os.close(descriptor)
    if os.path.lexists(path):
        raise AssertionError(f"cleanup node name remained after disposition: {path}")
    parent = _cleanup_lstat(path.parent)
    if parent[1] != "directory" or parent[2] != node.parent_identity:
        raise AssertionError(f"cleanup parent changed after disposition: {path.parent}")


def _delete_handle_bound_tree(batch_root: pathlib.Path) -> None:
    first = _capture_cleanup_tree(batch_root)
    second = _capture_cleanup_tree(batch_root)
    if first != second:
        raise AssertionError("cleanup tree changed between complete captures")
    if _CLEANUP_TEST_HOOK is not None:
        _CLEANUP_TEST_HOOK("after_tree_capture", batch_root)
    if os.name != "nt":
        raise AssertionError("handle-bound cleanup requires Windows; cleanup refused")

    root_node = next(node for node in second if node.relative == ".")
    root_parent_custody: Optional[_CleanupDirectoryCustody] = None
    directory_custodies: dict[str, _CleanupDirectoryCustody] = {}
    try:
        root_parent_custody = _acquire_cleanup_directory_custody(
            batch_root.parent,
            root_node.parent_identity,
        )
        for node in sorted(
            (item for item in second if item.kind == "directory"),
            key=lambda item: (item.depth, item.relative.casefold()),
        ):
            custody = _acquire_cleanup_directory_custody(
                _cleanup_path(batch_root, node),
                node.identity,
            )
            if custody.parent_identity != node.parent_identity:
                custody.close()
                raise AssertionError(
                    f"cleanup custody parent changed before delete: {custody.path.parent}"
                )
            directory_custodies[node.relative] = custody

        root_parent_custody.validate()
        for custody in directory_custodies.values():
            custody.validate()

        for node in sorted(
            second,
            key=lambda item: (item.depth, item.relative.casefold()),
            reverse=True,
        ):
            if node.relative == ".":
                parent_custody = root_parent_custody
            else:
                parent_relative = pathlib.PurePosixPath(node.relative).parent.as_posix()
                parent_custody = directory_custodies.get(parent_relative)
                if parent_custody is None:
                    raise AssertionError(
                        f"cleanup node lost parent custody: {_cleanup_path(batch_root, node)}"
                    )
            parent_custody.validate()
            if node.kind == "directory":
                own_custody = directory_custodies.pop(node.relative)
                own_custody.close()
            _delete_cleanup_node_windows(batch_root, node)
            parent_custody.validate()
        if os.path.lexists(batch_root):
            raise AssertionError(f"cleanup root remains after delete: {batch_root}")
    finally:
        for custody in reversed(tuple(directory_custodies.values())):
            custody.close()
        if root_parent_custody is not None:
            root_parent_custody.close()


def _delete_batch_fixtures(batch_root: pathlib.Path, fixtures: Sequence[pathlib.Path]) -> None:
    for fixture in fixtures:
        _assert_removable_batch_fixture(fixture, batch_root)
    _delete_handle_bound_tree(batch_root)


def _run_batch(
    round_root: pathlib.Path,
    batch_name: str,
    callback,
    *callback_args,
    **callback_kwargs,
) -> Any:
    if not os.path.lexists(round_root):
        raise AssertionError(f"batch parent is missing: {round_root}")
    round_metadata = os.lstat(round_root)
    if core.is_reparse_metadata(round_metadata) or not stat.S_ISDIR(round_metadata.st_mode):
        raise AssertionError(f"batch parent is unsafe: {round_root}")
    round_identity_before = core.node_identity(round_metadata)
    round_custody = _acquire_cleanup_directory_custody(
        round_root,
        round_identity_before,
    )
    batch_root = round_root / batch_name
    batch_custody: Optional[_CleanupDirectoryCustody] = None
    try:
        if os.path.lexists(batch_root):
            raise AssertionError(f"batch root already exists: {batch_root}")
        batch_root.mkdir()
        batch_metadata = os.lstat(batch_root)
        if core.is_reparse_metadata(batch_metadata) or not stat.S_ISDIR(
            batch_metadata.st_mode
        ):
            raise AssertionError(f"batch root is unsafe: {batch_root}")
        batch_identity_before = core.node_identity(batch_metadata)
        before = _snapshot_direct_children(batch_root)
        if before:
            raise AssertionError(f"new batch root was not empty: {batch_root}")
        batch_custody = _acquire_cleanup_directory_custody(
            batch_root,
            batch_identity_before,
        )

        try:
            result = callback(batch_root, *callback_args, **callback_kwargs)
        except Exception:
            # Callback failures retain the entire batch. Cleanup has not begun.
            raise

        round_custody.validate()
        batch_custody.validate()
        after = _snapshot_direct_children(batch_root)
        fixtures = _list_created_batch_children(batch_root, before, after)
        fixture_identities = {
            fixture: tuple(after[fixture.name])
            for fixture in fixtures
        }
        for fixture in fixtures:
            _assert_removable_batch_fixture(fixture, batch_root)
            if not os.path.lexists(fixture):
                raise AssertionError(f"batch cleanup fixture was removed: {fixture}")
            identity = tuple(core.node_identity(os.lstat(fixture)))
            if fixture_identities[fixture] != identity:
                raise AssertionError(f"batch fixture identity drifted before delete: {fixture}")

        batch_custody.close()
        batch_custody = None
        _delete_batch_fixtures(batch_root, fixtures)
        round_custody.validate()
        if not os.path.lexists(batch_root):
            return result
        raise AssertionError(f"batch cleanup did not remove batch root: {batch_root}")
    finally:
        if batch_custody is not None:
            batch_custody.close()
        round_custody.close()


def make_fixture(
    base: pathlib.Path,
    name: str,
    *,
    old_probe: bool = True,
    old_bridge: bool = True,
    archive_present: bool = False,
    run_id: Optional[str] = None,
) -> Fixture:
    # Scenario and fault-point labels remain complete in reports, but embedding
    # those labels verbatim in every TestRoot path can consume the production
    # legacy-path budget before the behavior under test is reached.  Use a
    # stable traceable basename for ordinary fixtures only.  The dedicated
    # 0/1/259/260 boundary tests construct their paths directly and therefore
    # do not pass through this compaction.
    fixture_name = fixture_basename(name)
    test_root = base / fixture_name
    oni = test_root / "oni"
    evidence = test_root / "evidence"
    release = test_root / "release"
    run_id = run_id or f"run-{fixture_name}"
    if len(run_id.encode("utf-16-le")) // 2 > MAX_FIXTURE_RUN_ID_UTF16_UNITS:
        raise AssertionError("fixture run id exceeds its UTF-16 budget")
    (oni / "mods" / "Dev").mkdir(parents=True)
    evidence.mkdir(parents=True)
    release.mkdir(parents=True)
    config_payloads = {
        "kplayerprefs.yaml": b"strings:\r\n  InstalledLanguage: en\r\n",
        "mods/mods.json": b'{"mods":[{"label":{"id":"baseline"},"enabled":true}]}\n',
        "mods/mods.json.bak": b'{"mods":[{"label":{"id":"baseline"},"enabled":true}]}\n',
    }
    for relative, payload in config_payloads.items():
        path = oni / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    save_dir = oni / "save_files" / "FixtureColony"
    save_dir.mkdir(parents=True)
    source_save = save_dir / "Original.sav"
    source_preview = source_save.with_suffix(".png")
    source_save.write_bytes(b"immutable-bootstrap-source-save\0")
    source_preview.write_bytes(b"immutable-bootstrap-source-preview\x89PNG")
    autosave = oni / "save_files" / "FixtureColony" / "AutoSave-001.sav"
    autosave_preview = autosave.with_suffix(".png")
    autosave.write_bytes(b"preexisting-autosave\0")
    autosave_preview.write_bytes(b"preexisting-autosave-preview\x89PNG")
    cloud_save = oni / "cloud_save_files" / "CloudFixture.sav"
    cloud_preview = cloud_save.with_suffix(".png")
    cloud_save.parent.mkdir(parents=True)
    cloud_save.write_bytes(b"preexisting-cloud-save\0")
    cloud_preview.write_bytes(b"preexisting-cloud-preview\x89PNG")
    loader_stem = f"{run_id}-loader"
    loader_save = save_dir / f"{loader_stem}.sav"
    loader_preview = loader_save.with_suffix(".png")
    shutil.copy2(source_save, loader_save)
    shutil.copy2(source_preview, loader_preview)
    if old_probe:
        probe = oni / pathlib.Path(*core.PROBE_RELATIVE.parts)
        probe.mkdir()
        (probe / "legacy.dll").write_bytes(b"old-probe")
        (probe / "legacy.json").write_text("old\n", encoding="utf-8")
    if old_bridge:
        bridge = oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
        bridge.mkdir()
        (bridge / "old-state.json").write_text('{"old":true}\n', encoding="utf-8")
        (bridge / "commands").mkdir()
    if archive_present:
        archive = oni / pathlib.Path(*core.ARCHIVE_ROOT_RELATIVE.parts)
        (archive / "historic").mkdir(parents=True)
        (archive / "historic" / "kept.txt").write_text("keep\n", encoding="utf-8")

    # Freeze parent-directory baselines well away from the current clock after
    # all fixture ONI children exist but before the manifest/install plan is
    # captured. Normal install renames then deterministically create a distinct
    # mtime that abort must restore. The Probe container (mods) has no direct
    # child rename in this transaction and is exercised by the dedicated
    # identity-preserving pre-abort setup instead.
    baseline_mtime = 1_700_000_000_000_000_000
    baseline_directories = [
        oni / pathlib.Path(*core.PROBE_RELATIVE.parts).parent,
        oni,
    ]
    if archive_present:
        baseline_directories.append(
            oni / pathlib.Path(*core.ARCHIVE_ROOT_RELATIVE.parts)
        )
    for index, directory in enumerate(baseline_directories):
        identity = core.node_identity(os.lstat(directory))
        core.set_directory_mtime_by_owned_handle(
            directory,
            identity,
            baseline_mtime + index * 10_000_000,
        )

    artifact_payloads = {
        "OniAiProbeMod.dll": b"prebuilt-release-dll-v1\0",
        "OniAiProbeMod.pdb": b"prebuilt-release-pdb-v1\0",
        "mod.yaml": (
            b'title: "ONI AI Probe"\n'
            b'description: "fixture"\n'
            b'staticID: "ass.OniAiProbe"\n'
        ),
        "mod_info.yaml": b"APIVersion: 2\n",
    }
    for artifact, payload in artifact_payloads.items():
        (release / artifact).write_bytes(payload)

    environment = evidence / run_id / "environment"
    environment.mkdir(parents=True)
    pre_boundary = evidence / "live-boundaries" / run_id / "pre-boundary.json"
    clone_manifest = evidence / "runtime" / run_id / "test-clone-manifest.json"
    def boundary_save_entry(path: pathlib.Path) -> dict[str, Any]:
        metadata = os.lstat(path)
        seconds, fraction = divmod(int(metadata.st_mtime_ns), 1_000_000_000)
        timestamp = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        scope = path.relative_to(oni).parts[0]
        if scope not in {"save_files", "cloud_save_files"}:
            raise AssertionError(f"fixture save escaped its declared scope: {path}")
        return {
            "scope": scope,
            "path_id": core.path_id(path),
            "parent_path_id": core.path_id(path.parent),
            "file_name_id": core.text_id(path.name),
            "extension": path.suffix.casefold(),
            "length": int(metadata.st_size),
            "last_write_utc": timestamp.strftime("%Y-%m-%dT%H:%M:%S")
            + f".{fraction:09d}+00:00",
            "sha256": core.sha256_bytes(path.read_bytes()),
            "stem_id": core.text_id(path.stem),
        }

    pre_saves = [
        boundary_save_entry(source_save),
        boundary_save_entry(source_preview),
        boundary_save_entry(autosave),
        boundary_save_entry(autosave_preview),
        boundary_save_entry(cloud_save),
        boundary_save_entry(cloud_preview),
    ]
    pre_saves.sort(key=lambda item: str(item["path_id"]))
    write_json(
        pre_boundary,
        {
            "schema": "oni.live-safety-boundary.v1",
            "run_id": run_id,
            "phase": "pre",
            "captured_at": "2026-08-31T00:00:00+00:00",
            "oni_root_id": core.path_id(oni),
            "path_disclosure": "identifiers_only",
            "saves": {"count": len(pre_saves), "entries": pre_saves},
            "mod_configs": {"count": 0, "entries": []},
            "installed_probe": {"count": 0, "entries": []},
            "bridge_policies": {"count": 0, "entries": []},
            "queues": {
                **{stage: 0 for stage in core.COMMAND_QUEUES},
                "temporary_files": 0,
            },
            "processes": {
                "oxygen_not_included_count": 0,
                "bridge_count": 0,
            },
        },
    )
    write_json(
        clone_manifest,
        {
            "schema": "oni.named-test-clone.v1",
            "run_id": run_id,
            "source_save_sha256": core.sha256_bytes(source_save.read_bytes()),
            "loader_save_sha256": core.sha256_bytes(loader_save.read_bytes()),
        },
    )
    probe_path = oni / pathlib.Path(*core.PROBE_RELATIVE.parts)
    bridge_path = oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
    archive_path = oni / pathlib.Path(*core.ARCHIVE_ROOT_RELATIVE.parts)
    probe_snapshot = core.snapshot_tree(probe_path)
    bridge_snapshot = core.snapshot_tree(bridge_path)
    archive_snapshot = core.snapshot_tree(archive_path)
    config_records: list[dict[str, Any]] = []
    for index, relative in enumerate(core.CONFIG_RELATIVES):
        live = oni / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        record: dict[str, Any] = {
            "relative": relative,
            "path_id_lower": core.path_id(live),
            "path_id_upper": core.path_id(live, upper=True),
            "exists": live.is_file(),
            "backup_relative": None,
        }
        if live.is_file():
            backup_relative = f"backup/configs/{index:02d}-{live.name}"
            backup = environment / pathlib.Path(
                *pathlib.PurePosixPath(backup_relative).parts
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, backup)
            record["state"] = core.compact_file_state(live)
            record["backup_relative"] = backup_relative
        config_records.append(record)
    if old_probe:
        shutil.copytree(probe_path, environment / "backup" / "probe", copy_function=shutil.copy2)
    if old_bridge:
        shutil.copytree(
            bridge_path,
            environment / "backup" / "old-bridge",
            copy_function=shutil.copy2,
        )
    source_state = core.compact_file_state(source_save)
    source_preview_state = core.compact_file_state(source_preview)
    loader_state = core.compact_file_state(loader_save)
    loader_preview_state = core.compact_file_state(loader_preview)
    manifest: dict[str, Any] = {
        "schema": core.ENVIRONMENT_SCHEMA,
        "run_id": run_id,
        "created_at": "2026-08-31T00:00:00+00:00",
        "mode": "test_fixture",
        "path_disclosure": "root_ids_and_relative_paths_only",
        "roots": {
            "oni_root_id_lower": core.path_id(oni),
            "oni_root_id_upper": core.path_id(oni, upper=True),
            "evidence_root_id_lower": core.path_id(evidence),
            "evidence_root_id_upper": core.path_id(evidence, upper=True),
            "same_local_volume": True,
        },
        "bindings": {
            "pre_boundary": {
                "path_id": core.path_id(pre_boundary),
                "sha256": core.sha256_bytes(pre_boundary.read_bytes()),
            },
            "clone_manifest": {
                "path_id": core.path_id(clone_manifest),
                "sha256": core.sha256_bytes(clone_manifest.read_bytes()),
            },
            "source_save": {
                "path_id_lower": core.path_id(source_save),
                "path_id_upper": core.path_id(source_save, upper=True),
                "state": source_state,
            },
            "source_preview": {
                "path_id_lower": core.path_id(source_preview),
                "path_id_upper": core.path_id(source_preview, upper=True),
                "state": source_preview_state,
            },
            "loader_save": {
                "path_id_lower": core.path_id(loader_save),
                "path_id_upper": core.path_id(loader_save, upper=True),
                "stem_id": core.text_id(loader_stem),
                "state": loader_state,
            },
            "loader_preview": {
                "path_id_lower": core.path_id(loader_preview),
                "path_id_upper": core.path_id(loader_preview, upper=True),
                "state": loader_preview_state,
            },
        },
        "backup": {
            "configs": config_records,
            "probe": {
                "relative": core.PROBE_RELATIVE.as_posix(),
                "path_id_lower": core.path_id(probe_path),
                "path_id_upper": core.path_id(probe_path, upper=True),
                "backup_relative": "backup/probe" if old_probe else None,
                "snapshot": probe_snapshot,
            },
            "old_bridge": {
                "relative": core.BRIDGE_RELATIVE.as_posix(),
                "path_id_lower": core.path_id(bridge_path),
                "path_id_upper": core.path_id(bridge_path, upper=True),
                "backup_relative": "backup/old-bridge" if old_bridge else None,
                "snapshot": bridge_snapshot,
            },
            "bridge_archive_root": {
                "relative": core.ARCHIVE_ROOT_RELATIVE.as_posix(),
                "path_id_lower": core.path_id(archive_path),
                "path_id_upper": core.path_id(archive_path, upper=True),
                "snapshot": archive_snapshot,
            },
        },
        "preconditions": {
            "processes": {"OxygenNotIncluded": 0, "oni_bridge.py": 0},
            "bridge_gate": {},
        },
    }
    manifest_path = environment / "environment-manifest.json"
    write_json(manifest_path, manifest)
    manifest_hash = core.sha256_bytes(manifest_path.read_bytes())
    create_environment_journal(
        environment / "restore-journal.jsonl",
        manifest_hash,
        pre_boundary_hash=core.sha256_bytes(pre_boundary.read_bytes()),
        clone_manifest_hash=core.sha256_bytes(clone_manifest.read_bytes()),
    )
    process_fixture = test_root / "processes.json"
    write_json(process_fixture, {"OxygenNotIncluded": 0, "oni_bridge.py": 0})
    release_hash = core.sha256_bytes((release / "OniAiProbeMod.dll").read_bytes())
    arguments = [
        "--run-id",
        run_id,
        "--oni-root",
        str(oni),
        "--evidence-root",
        str(evidence),
        "--environment-manifest",
        str(manifest_path),
        "--expected-environment-manifest-sha256",
        manifest_hash,
        "--release-dll",
        str(release / "OniAiProbeMod.dll"),
        "--expected-release-dll-sha256",
        release_hash,
        "--expected-mod-version",
        core.EXPECTED_MOD_VERSION,
        "--confirm-live-run-id",
        run_id,
        "--test-root",
        str(test_root),
        "--process-state-fixture",
        str(process_fixture),
    ]
    stage_nonce = core.sha256_bytes(f"{name}:stage-nonce".encode("utf-8"))
    stage_nonce = stage_nonce.removeprefix("sha256:")[:32]
    abort_stage_nonce = core.sha256_bytes(
        f"{name}:abort-stage-nonce".encode("utf-8")
    ).removeprefix("sha256:")[:32]
    return Fixture(
        test_root,
        oni,
        evidence,
        release,
        run_id,
        manifest_path,
        manifest_hash,
        release_hash,
        process_fixture,
        pre_boundary,
        clone_manifest,
        source_save,
        loader_save,
        loader_stem,
        arguments,
        stage_nonce,
        abort_stage_nonce=abort_stage_nonce,
    )


def _invoke_mode(
    fixture: Fixture,
    mode: str,
    *,
    fail_at: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if mode not in {"what-if", "stage", "recover-stage", "apply"}:
        raise AssertionError(f"unknown bootstrap verifier mode: {mode}")
    command = [
        sys.executable,
        str(MANAGER),
        *fixture.args,
        "--operation",
        "install",
        "--" + mode,
        "--stage-nonce",
        fixture.stage_nonce,
    ]
    if mode in {"stage", "recover-stage", "apply"}:
        if not fixture.expected_stage_sha256 or not fixture.expected_transaction_id:
            raise AssertionError(f"{mode} invoked without a frozen WhatIf stage binding")
        command.extend(
            (
                "--expected-stage-sha256",
                fixture.expected_stage_sha256,
                "--expected-transaction-id",
                fixture.expected_transaction_id,
            )
        )
    if fail_at:
        command.extend(("--test-fail-at", fail_at))
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = {"status": "invalid_output", "stdout": completed.stdout[-1000:]}
    return completed, value


def forecast_install_stage(
    fixture: Fixture,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    completed, value = _invoke_mode(fixture, "what-if")
    if completed.returncode == 0:
        stage_sha256 = value.get("expected_stage_sha256")
        transaction_id = value.get("transaction_id")
        if (
            not isinstance(stage_sha256, str)
            or not stage_sha256.startswith("sha256:")
            or not isinstance(transaction_id, str)
            or not transaction_id
            or value.get("stage_nonce") != fixture.stage_nonce
            or value.get("write_count") != 0
        ):
            raise AssertionError(f"WhatIf returned an invalid stage binding: {value}")
        fixture.expected_stage_sha256 = stage_sha256
        fixture.expected_transaction_id = transaction_id
    return completed, value


def stage_fixture(
    fixture: Fixture,
    *,
    fail_at: str = "",
    recover: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if not fixture.expected_stage_sha256 or not fixture.expected_transaction_id:
        forecast, forecast_json = forecast_install_stage(fixture)
        if forecast.returncode != 0:
            return forecast, forecast_json
    return _invoke_mode(
        fixture,
        "recover-stage" if recover else "stage",
        fail_at=fail_at,
    )


def invoke(
    fixture: Fixture,
    *,
    apply: bool,
    fail_at: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if not apply:
        return _invoke_mode(fixture, "what-if", fail_at=fail_at)
    stage_path = fixture.manifest.parent / "runtime-bootstrap-install-stage.json"
    if not os.path.lexists(stage_path):
        staged, staged_json = stage_fixture(fixture)
        if staged.returncode != 0:
            return staged, staged_json
    elif not fixture.expected_stage_sha256 or not fixture.expected_transaction_id:
        forecast, forecast_json = forecast_install_stage(fixture)
        if forecast.returncode != 0:
            return forecast, forecast_json
    return _invoke_mode(fixture, "apply", fail_at=fail_at)


def transaction_dir(fixture: Fixture) -> pathlib.Path:
    return fixture.evidence / fixture.run_id / "runtime-bootstrap"


def parse_apply_args(fixture: Fixture) -> argparse.Namespace:
    stage_path = fixture.manifest.parent / "runtime-bootstrap-install-stage.json"
    if not os.path.lexists(stage_path):
        staged, staged_json = stage_fixture(fixture)
        if staged.returncode != 0:
            raise AssertionError(f"fixture stage did not publish: {staged_json}")
    if not fixture.expected_stage_sha256 or not fixture.expected_transaction_id:
        raise AssertionError("fixture has no external stage binding")
    return core.build_parser().parse_args(
        [
            *fixture.args,
            "--operation",
            "install",
            "--apply",
            "--stage-nonce",
            fixture.stage_nonce,
            "--expected-stage-sha256",
            fixture.expected_stage_sha256,
            "--expected-transaction-id",
            fixture.expected_transaction_id,
        ]
    )


def parse_what_if_args(fixture: Fixture) -> argparse.Namespace:
    return core.build_parser().parse_args(
        [
            *fixture.args,
            "--operation",
            "install",
            "--what-if",
            "--stage-nonce",
            fixture.stage_nonce,
        ]
    )


def _abort_common_args(fixture: Fixture) -> list[str]:
    if not fixture.install_result_sha256:
        result_path = transaction_dir(fixture) / "result.json"
        if not result_path.is_file():
            raise AssertionError("abort fixture has no externally hashed installed_ready result")
        fixture.install_result_sha256 = core.sha256_bytes(result_path.read_bytes())
    return [
        *fixture.args,
        "--operation",
        "abort",
        "--expected-install-result-sha256",
        fixture.install_result_sha256,
        "--pre-boundary",
        str(fixture.pre_boundary),
        "--clone-manifest",
        str(fixture.clone_manifest),
        "--source-save",
        str(fixture.source_save),
        "--loader-save",
        str(fixture.loader_save),
        "--loader-stem",
        fixture.loader_stem,
    ]


def _invoke_abort_mode(
    fixture: Fixture,
    mode: str,
    *,
    fail_at: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if mode not in {"what-if", "stage", "recover-stage", "apply"}:
        raise AssertionError(f"unknown abort verifier mode: {mode}")
    command = [
        sys.executable,
        str(MANAGER),
        *_abort_common_args(fixture),
        "--" + mode,
        "--stage-nonce",
        fixture.abort_stage_nonce,
    ]
    if mode in {"stage", "recover-stage", "apply"}:
        if not fixture.expected_abort_stage_sha256 or not fixture.expected_abort_id:
            raise AssertionError(f"abort {mode} invoked without frozen WhatIf bindings")
        command.extend(
            (
                "--expected-stage-sha256",
                fixture.expected_abort_stage_sha256,
                "--expected-abort-id",
                fixture.expected_abort_id,
            )
        )
    if fail_at:
        command.extend(("--test-fail-at", fail_at))
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = {"status": "invalid_output", "stdout": completed.stdout[-1000:]}
    return completed, value


def forecast_abort_stage(
    fixture: Fixture,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    completed, value = _invoke_abort_mode(fixture, "what-if")
    if completed.returncode == 0:
        stage_sha256 = value.get("expected_stage_sha256")
        abort_id = value.get("abort_id")
        if (
            not isinstance(stage_sha256, str)
            or not stage_sha256.startswith("sha256:")
            or not isinstance(abort_id, str)
            or not abort_id
            or value.get("stage_nonce") != fixture.abort_stage_nonce
            or value.get("write_count") != 0
        ):
            raise AssertionError(f"abort WhatIf returned invalid stage bindings: {value}")
        fixture.expected_abort_stage_sha256 = stage_sha256
        fixture.expected_abort_id = abort_id
    return completed, value


def stage_abort_fixture(
    fixture: Fixture,
    *,
    fail_at: str = "",
    recover: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if not fixture.expected_abort_stage_sha256 or not fixture.expected_abort_id:
        forecast, forecast_json = forecast_abort_stage(fixture)
        if forecast.returncode != 0:
            return forecast, forecast_json
    return _invoke_abort_mode(
        fixture,
        "recover-stage" if recover else "stage",
        fail_at=fail_at,
    )


def invoke_abort(
    fixture: Fixture,
    *,
    apply: bool,
    fail_at: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if not apply:
        return _invoke_abort_mode(fixture, "what-if", fail_at=fail_at)
    stage_path = fixture.manifest.parent / "runtime-bootstrap-abort-stage.json"
    if not os.path.lexists(stage_path):
        staged, staged_json = stage_abort_fixture(fixture)
        if staged.returncode != 0:
            return staged, staged_json
    elif not fixture.expected_abort_stage_sha256 or not fixture.expected_abort_id:
        forecast, forecast_json = forecast_abort_stage(fixture)
        if forecast.returncode != 0:
            return forecast, forecast_json
    return _invoke_abort_mode(fixture, "apply", fail_at=fail_at)


def parse_abort_apply_args(fixture: Fixture) -> argparse.Namespace:
    stage_path = fixture.manifest.parent / "runtime-bootstrap-abort-stage.json"
    if not os.path.lexists(stage_path):
        staged, staged_json = stage_abort_fixture(fixture)
        if staged.returncode != 0:
            raise AssertionError(f"abort fixture stage failed: {staged_json}")
    if not fixture.expected_abort_stage_sha256 or not fixture.expected_abort_id:
        raise AssertionError("abort fixture has no external stage binding")
    return core.build_parser().parse_args(
        [
            *_abort_common_args(fixture),
            "--apply",
            "--stage-nonce",
            fixture.abort_stage_nonce,
            "--expected-stage-sha256",
            fixture.expected_abort_stage_sha256,
            "--expected-abort-id",
            fixture.expected_abort_id,
        ]
    )


def optional_bound_tree_fingerprint(path: pathlib.Path) -> str:
    if not os.path.lexists(path):
        return core.canonical_hash({"exists": False})
    snapshot, identities = core.capture_bound_tree(path)
    return core.canonical_hash(
        {"exists": True, "snapshot": snapshot, "identities": identities}
    )


def runtime_fingerprint(fixture: Fixture) -> str:
    """Bind every runtime path that install/abort is allowed to mutate."""

    return core.canonical_hash(
        {
            "probe": optional_bound_tree_fingerprint(
                fixture.oni / pathlib.Path(*core.PROBE_RELATIVE.parts)
            ),
            "bridge": optional_bound_tree_fingerprint(
                fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
            ),
            "archive": optional_bound_tree_fingerprint(
                fixture.oni / pathlib.Path(*core.ARCHIVE_ROOT_RELATIVE.parts)
            ),
        }
    )


def oni_tree_fingerprint(fixture: Fixture) -> str:
    return optional_bound_tree_fingerprint(fixture.oni)


def assert_unique_loader_pair_matches_source(fixture: Fixture) -> None:
    save_dir = fixture.loader_save.parent
    expected = {
        fixture.loader_save.name,
        fixture.loader_save.with_suffix(".png").name,
    }
    matching = {
        path.name
        for path in save_dir.iterdir()
        if path.is_file() and path.stem.casefold() == fixture.loader_stem.casefold()
    }
    if matching != expected:
        raise AssertionError(f"loader clone pathset is not the exact named pair: {matching}")
    if (
        fixture.loader_save.read_bytes() != fixture.source_save.read_bytes()
        or fixture.loader_save.with_suffix(".png").read_bytes()
        != fixture.source_save.with_suffix(".png").read_bytes()
    ):
        raise AssertionError("loader clone bytes do not equal immutable source pair")


def assert_no_ready_result(fixture: Fixture, label: str) -> None:
    result_path = transaction_dir(fixture) / "result.json"
    if os.path.lexists(result_path):
        raise AssertionError(f"{label} left an externally visible installed_ready result")


def assert_no_abort_result(fixture: Fixture, label: str) -> None:
    result_path = transaction_dir(fixture) / "abort" / "result.json"
    if os.path.lexists(result_path):
        raise AssertionError(f"{label} left an externally visible aborted_ready result")


def assert_no_live_environment_lock(fixture: Fixture, label: str) -> None:
    live = fixture.evidence / fixture.run_id / "environment" / ".finalize.lock"
    if os.path.lexists(live):
        raise AssertionError(f"{label} left a live shared environment lock")


def expect_direct_blocked(
    fixture: Fixture,
    label: str,
    hook: Optional[Callable[[str], None]] = None,
) -> str:
    try:
        core.execute(parse_apply_args(fixture), hook=hook)
    except core.BootstrapError as exc:
        return str(exc)
    else:
        raise AssertionError(f"{label} was not blocked")


def published_plan(fixture: Fixture) -> tuple[core.Roots, dict[str, Any]]:
    parsed = parse_what_if_args(fixture)
    roots = core.resolve_roots(parsed)
    plan = json.loads(
        (transaction_dir(fixture) / "plan.json").read_text(encoding="utf-8")
    )
    return roots, plan


def deterministic_unpublished_install_contract(
    fixture: Fixture,
) -> tuple[core.Roots, dict[str, Any], dict[str, Any]]:
    """Rebuild the public deterministic contract without publishing a latch."""

    parsed = parse_what_if_args(fixture)
    roots = core.resolve_roots(parsed)
    manifest, environment_binding, _journal = core.load_environment(parsed, roots)
    releases, _payloads = core.release_artifacts(parsed, roots)
    intent, plan = core.capture_initial_plan(
        parsed,
        roots,
        manifest,
        environment_binding,
        releases,
    )
    return roots, intent, plan


def deterministic_unpublished_abort_contract(
    fixture: Fixture,
) -> tuple[core.Roots, dict[str, Any], dict[str, Any]]:
    parsed = core.build_parser().parse_args(
        [
            *_abort_common_args(fixture),
            "--what-if",
            "--stage-nonce",
            fixture.abort_stage_nonce,
        ]
    )
    roots = core.resolve_roots(parsed)
    manifest, environment_binding, _journal = core.load_environment(parsed, roots)
    install_plan, _ = core.stable_json(
        roots.transaction_dir / "plan.json", "runtime bootstrap install plan"
    )
    install_intent, _ = core.stable_json(
        roots.transaction_dir / "intent.json", "runtime bootstrap install intent"
    )
    releases, _payloads = core.release_artifacts(parsed, roots)
    core.validate_deterministic_contract(
        parsed,
        roots,
        manifest,
        environment_binding,
        releases,
        install_intent,
        install_plan,
    )
    install_result, install_state = core._load_install_ready_for_abort(
        parsed,
        roots,
        install_plan,
    )
    explicit = core._abort_explicit_paths(parsed, roots, manifest)
    intent, plan = core._capture_abort_contract(
        parsed,
        roots,
        manifest,
        install_plan,
        install_result,
        install_state,
        explicit,
    )
    return roots, intent, plan


def seed_external_lock_attempt(
    fixture: Fixture,
    *,
    purpose: str = "install",
    owner_mode: str = "complete",
    stage_mode: str = "none",
) -> pathlib.Path:
    """Build an attacker-controlled attempt without using manager acquisition.

    The helper intentionally uses the public deterministic contracts.  A safe
    implementation must not infer ownership merely because an attacker can
    reproduce bytes and self-hashes under an already owned parent directory.
    """

    roots, plan = published_plan(fixture)
    attempt_root = transaction_dir(fixture) / "environment-lock-attempts"
    epoch = len(list(attempt_root.iterdir()))
    attempt = attempt_root / f"{epoch:04d}-{purpose}"
    attempt.mkdir()
    owner = core._lock_attempt_owner_contract(
        plan, epoch, purpose, core.node_identity(os.lstat(attempt))
    )
    owner_payload = core.document_bytes(owner)
    if owner_mode == "complete":
        (attempt / "owner.json").write_bytes(owner_payload)
    elif owner_mode == "partial":
        (attempt / "owner.json.new").write_bytes(owner_payload[: max(1, len(owner_payload) // 2)])
    elif owner_mode != "missing":
        raise AssertionError(f"unknown owner_mode {owner_mode}")
    if owner_mode == "complete":
        intent, payload = core._lock_attempt_contract(roots, plan, epoch, purpose)
        (attempt / "intent.json").write_bytes(core.document_bytes(intent))
        if stage_mode == "unclaimed_empty":
            (attempt / "lock.stage.new").write_bytes(b"")
        elif stage_mode == "unclaimed_full":
            (attempt / "lock.stage.new").write_bytes(payload)
        elif stage_mode == "partial_claim":
            stage = attempt / "lock.stage.new"
            stage.write_bytes(payload)
            state = core.stable_file(stage, max_bytes=core.MAX_JSON_BYTES, collect=True)
            claim = core._lock_claim_contract(intent, payload, state["identity"])
            claim_payload = core.document_bytes(claim)
            (attempt / "claim.json.new").write_bytes(
                claim_payload[: max(1, len(claim_payload) // 2)]
            )
        elif stage_mode != "none":
            raise AssertionError(f"unknown stage_mode {stage_mode}")
    return attempt


def assert_installed(fixture: Fixture, result: Mapping[str, Any]) -> None:
    if result.get("status") not in {"installed_ready", "already_installed"}:
        raise AssertionError(f"transaction did not converge: {result}")
    probe = fixture.oni / pathlib.Path(*core.PROBE_RELATIVE.parts)
    bridge = fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
    if sorted(path.name for path in probe.iterdir()) != sorted(core.ARTIFACT_NAMES):
        raise AssertionError("installed Probe is not the exact four-file release")
    expected_directories = {
        "commands",
        *(f"commands/{name}" for name in core.COMMAND_QUEUES),
        "validation-controls",
        *(f"validation-controls/{name}" for name in core.CONTROL_QUEUES),
        "evidence",
    }
    actual_directories = {
        path.relative_to(bridge).as_posix() for path in bridge.rglob("*") if path.is_dir()
    }
    if actual_directories != expected_directories:
        raise AssertionError("installed Bridge queues/directories are not exact")
    action = json.loads((bridge / "action-policy.json").read_text(encoding="utf-8"))
    controls = json.loads(
        (bridge / "validation-control-policy.json").read_text(encoding="utf-8")
    )
    bootstrap = json.loads(
        (bridge / "v27-runtime-bootstrap.json").read_text(encoding="utf-8")
    )
    if (
        action.get("actions_enabled") is not False
        or action.get("suggestions_enabled") is not False
        or action.get("allowed_actions") != ["focus_note", "noop"]
        or controls.get("controls_enabled") is not False
        or bootstrap.get("schema") != core.BOOTSTRAP_SCHEMA
        or bootstrap.get("run_id") != fixture.run_id
        or bootstrap.get("installed_release") is not True
    ):
        raise AssertionError("installed fail-closed policy/bootstrap contract is invalid")
    if any(path.name.endswith(".new") for path in transaction_dir(fixture).rglob("*")):
        raise AssertionError("successful transaction left a temporary publication")
    assert_no_live_environment_lock(fixture, "successful runtime bootstrap")
    transaction = transaction_dir(fixture)
    plan = json.loads((transaction / "plan.json").read_text(encoding="utf-8"))
    published_result = json.loads(
        (transaction / "result.json").read_text(encoding="utf-8")
    )
    if len(INSTALL_RESULT_KEYS) != 38 or set(published_result) != INSTALL_RESULT_KEYS:
        raise AssertionError("installed_ready result is not its exact 38-key contract")
    old_probe_expected = plan.get("prestate", {}).get("probe", {}).get(
        "snapshot", {}
    ).get("exists") is True
    if (
        old_probe_expected
        and published_result.get("old_probe_recovery_relative")
        != "recovery/old-probe"
    ) or (
        not old_probe_expected
        and published_result.get("old_probe_recovery_relative") is not None
    ):
        raise AssertionError("installed_ready old-Probe recovery projection is invalid")
    fixture.install_result_sha256 = core.sha256_bytes(
        (transaction / "result.json").read_bytes()
    )
    roots = core.resolve_roots(parse_apply_args(fixture))
    core.validate_result(
        roots,
        plan,
        published_result,
        require_ready_released=True,
    )
    candidate = json.loads(
        (transaction / "result-candidate.json").read_text(encoding="utf-8")
    )
    if (
        candidate.get("schema") != core.RESULT_CANDIDATE_SCHEMA
        or candidate.get("status") != "candidate_not_ready"
        or published_result.get("schema") != core.RESULT_SCHEMA
        or published_result.get("status") != "installed_ready"
    ):
        raise AssertionError("candidate/ready two-phase result contract is invalid")


def assert_aborted(
    fixture: Fixture,
    result: Mapping[str, Any],
    *,
    expected_oni_fingerprint: str,
    expected_clone_manifest_state: Mapping[str, Any],
) -> None:
    if result.get("status") not in {"aborted_ready", "already_aborted"}:
        raise AssertionError(f"pre-config abort did not converge: {result}")
    if oni_tree_fingerprint(fixture) != expected_oni_fingerprint:
        raise AssertionError("pre-config abort did not restore the exact ONI TestRoot tree")
    assert_unique_loader_pair_matches_source(fixture)
    clone_state = core.stable_file(
        fixture.clone_manifest,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    if not core.exact_json_equal(clone_state, expected_clone_manifest_state):
        raise AssertionError("pre-config abort changed the clone-manifest bytes/identity")
    if not fixture.install_result_sha256 or core.sha256_bytes(
        (transaction_dir(fixture) / "result.json").read_bytes()
    ) != fixture.install_result_sha256:
        raise AssertionError("pre-config abort changed its installed_ready external anchor")
    abort_root = transaction_dir(fixture) / "abort"
    install_plan = json.loads(
        (transaction_dir(fixture) / "plan.json").read_text(encoding="utf-8")
    )
    abort_plan = json.loads((abort_root / "plan.json").read_text(encoding="utf-8"))
    published = json.loads((abort_root / "result.json").read_text(encoding="utf-8"))
    core._validate_abort_result(
        core.resolve_roots(parse_abort_apply_args(fixture)),
        install_plan,
        abort_plan,
        published,
        require_ready_released=True,
    )
    assert_no_live_environment_lock(fixture, "successful pre-config abort")
    if any(path.name.endswith(".new") for path in abort_root.rglob("*")):
        raise AssertionError("successful pre-config abort left a temporary publication")


def prepare_installed_staged_abort(
    fixture: Fixture,
) -> tuple[str, dict[str, Any], str]:
    oni_before_install = oni_tree_fingerprint(fixture)
    clone_before = core.stable_file(
        fixture.clone_manifest,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(f"TestRoot runtime install failed: {installed_json}")
    assert_installed(fixture, installed_json)
    installed_runtime = runtime_fingerprint(fixture)
    forecast, forecast_json = forecast_abort_stage(fixture)
    if forecast.returncode != 0:
        raise AssertionError(f"pre-config abort WhatIf failed: {forecast_json}")
    staged, staged_json = stage_abort_fixture(fixture)
    if staged.returncode != 0:
        raise AssertionError(f"pre-config abort stage failed: {staged_json}")
    return oni_before_install, clone_before, installed_runtime


def force_abort_mtime_restore_path(
    fixture: Fixture,
    role: str,
) -> None:
    """Create a TestRoot-only, identity-preserving mtime restore obligation.

    Some ancestor directories are intentionally included in the abort plan as
    custody guards even when a particular filesystem run does not naturally
    alter their root mtime. Fault hooks around the physical SetFileTime step
    must nevertheless be exercised. This setup runs after install but before
    abort WhatIf/stage, uses the same inode-bound handle primitive as product,
    and proves that only the root mtime changed.
    """

    install_plan, _ = core.stable_json(
        transaction_dir(fixture) / "plan.json",
        "mtime fault fixture install plan",
    )
    parent_prestate = install_plan["prestate"]["parent_directories"]
    if role == "probe_parent":
        path = fixture.oni / pathlib.Path(*core.PROBE_RELATIVE.parts).parent
        expected_identity = parent_prestate[role]["identity"]
        restored_value = int(parent_prestate[role]["last_write_ns"])
    elif role == "probe_container":
        path = fixture.oni / pathlib.Path(*core.PROBE_RELATIVE.parts).parent.parent
        expected_identity = parent_prestate[role]["identity"]
        restored_value = int(parent_prestate[role]["last_write_ns"])
    elif role == "oni_root":
        path = fixture.oni
        expected_identity = parent_prestate[role]["identity"]
        restored_value = int(parent_prestate[role]["last_write_ns"])
    elif role == "archive_root":
        path = fixture.oni / pathlib.Path(*core.ARCHIVE_ROOT_RELATIVE.parts)
        expected_identity = install_plan["prestate"]["archive_root"]["identity"][
            "root_identity"
        ]
        restored_value = int(
            install_plan["prestate"]["archive_root"]["snapshot"][
                "root_last_write_ns"
            ]
        )
    else:
        raise AssertionError(f"unknown abort mtime fault role: {role}")
    before_snapshot, before_identity = core.capture_bound_tree(path)
    if not core.exact_json_equal(
        before_identity.get("root_identity"), expected_identity
    ):
        raise AssertionError(f"mtime fault fixture {role} lost its planned inode")
    drifted_value = restored_value + 30_000_000_000
    if drifted_value == before_snapshot.get("root_last_write_ns"):
        drifted_value += 10_000_000_000
    core.set_directory_mtime_by_owned_handle(
        path,
        expected_identity,
        drifted_value,
    )
    after_snapshot, after_identity = core.capture_bound_tree(path)
    before_without_root_mtime = dict(before_snapshot)
    after_without_root_mtime = dict(after_snapshot)
    before_without_root_mtime.pop("root_last_write_ns", None)
    after_without_root_mtime.pop("root_last_write_ns", None)
    if (
        after_snapshot.get("root_last_write_ns") != drifted_value
        or drifted_value == restored_value
        or not core.exact_json_equal(before_identity, after_identity)
        or not core.exact_json_equal(
            before_without_root_mtime, after_without_root_mtime
        )
    ):
        raise AssertionError(
            f"mtime fault fixture {role} changed more than root mtime"
        )


def tree_fingerprint(path: pathlib.Path) -> str:
    snapshot, identities = core.capture_bound_tree(path)
    return core.canonical_hash({"snapshot": snapshot, "identity": identities})


def transaction_evidence_fingerprint_while_locked(fixture: Fixture) -> str:
    """Fingerprint transaction evidence without opening its held lock file."""

    root = transaction_dir(fixture)
    records: list[dict[str, Any]] = []
    root_metadata = os.lstat(root)
    if core.is_reparse_metadata(root_metadata) or not root.is_dir():
        raise AssertionError("transaction evidence root is unsafe")
    records.append(
        {
            "relative": ".",
            "kind": "directory",
            "identity": core.node_identity(root_metadata),
            "last_write_ns": int(root_metadata.st_mtime_ns),
        }
    )
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        metadata = os.lstat(path)
        if core.is_reparse_metadata(metadata):
            raise AssertionError(f"transaction evidence contains reparse node {relative}")
        if path.is_dir():
            records.append(
                {
                    "relative": relative,
                    "kind": "directory",
                    "identity": core.node_identity(metadata),
                    "last_write_ns": int(metadata.st_mtime_ns),
                }
            )
        elif relative == "transaction.lock":
            # The manager intentionally keeps this file open without read
            # sharing while invoking the cooperative-lock hook.  lstat is
            # sufficient to prove the inode itself was not replaced.
            records.append(
                {
                    "relative": relative,
                    "kind": "held_lock_file",
                    "identity": core.node_identity(metadata),
                    "length": int(metadata.st_size),
                    "last_write_ns": int(metadata.st_mtime_ns),
                }
            )
        else:
            state = core.stable_file(
                path,
                max_bytes=core.MAX_ARTIFACT_BYTES,
                collect=False,
            )
            records.append(
                {
                    "relative": relative,
                    "kind": "file",
                    "state": state,
                }
            )
    return core.canonical_hash({"records": records})


def run_stage_authorization_cases(
    base: pathlib.Path, round_index: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for point in STAGE_FAULT_POINTS:
        fixture = make_fixture(base, f"r{round_index}-stage-fault-{point}")
        oni_before = oni_tree_fingerprint(fixture)
        forecast, forecast_json = forecast_install_stage(fixture)
        if forecast.returncode != 0:
            raise AssertionError(f"stage WhatIf failed: {forecast_json}")
        interrupted, interrupted_json = stage_fixture(fixture, fail_at=point)
        if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
            raise AssertionError(
                f"stage fault {point} did not interrupt at its publication: {interrupted_json}"
            )
        stage_path = fixture.manifest.parent / "runtime-bootstrap-install-stage.json"
        if (
            not stage_path.is_file()
            or core.sha256_bytes(stage_path.read_bytes())
            != fixture.expected_stage_sha256
            or os.path.lexists(transaction_dir(fixture))
            or oni_tree_fingerprint(fixture) != oni_before
        ):
            raise AssertionError(
                "pre-return stage crash reached support/ONI state or lost its external bundle"
            )
        retried, retried_json = stage_fixture(fixture, recover=True)
        if (
            retried.returncode != 0
            or retried_json.get("status") != "stage_recovered"
            or retried_json.get("stage_sha256") != fixture.expected_stage_sha256
            or retried_json.get("transaction_id") != fixture.expected_transaction_id
            or retried_json.get("runtime_write_count") != 0
            or os.path.lexists(transaction_dir(fixture))
            or oni_tree_fingerprint(fixture) != oni_before
        ):
            raise AssertionError(f"externally anchored stage retry diverged: {retried_json}")
        installed, installed_json = invoke(fixture, apply=True)
        if installed.returncode != 0:
            raise AssertionError(
                f"stage-authorized apply did not converge after retry: {installed_json}"
            )
        assert_installed(fixture, installed_json)
        results.append(
            {
                "name": f"stage_pre_return_crash_retry:{point}",
                "status": "passed",
                "classification": "workspace_only_crash_recovery",
            }
        )

    fixture = make_fixture(base, f"r{round_index}-stage-wrong-external-hash")
    oni_before = oni_tree_fingerprint(fixture)
    forecast, forecast_json = forecast_install_stage(fixture)
    if forecast.returncode != 0:
        raise AssertionError(f"wrong-stage-hash forecast failed: {forecast_json}")
    fixture.expected_stage_sha256 = "sha256:" + "f" * 64
    refused, refused_json = stage_fixture(fixture)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or os.path.lexists(fixture.manifest.parent / "runtime-bootstrap-install-stage.json")
        or os.path.lexists(transaction_dir(fixture))
        or oni_tree_fingerprint(fixture) != oni_before
    ):
        raise AssertionError(f"wrong external stage hash was not zero-write: {refused_json}")
    results.append(
        {
            "name": "stage_wrong_external_hash_refused_before_publication",
            "status": "passed",
            "classification": "external_anchor_attack",
        }
    )

    fixture = make_fixture(base, f"r{round_index}-stage-exact-preplant")
    oni_before = oni_tree_fingerprint(fixture)
    forecast, forecast_json = forecast_install_stage(fixture)
    if forecast.returncode != 0:
        raise AssertionError(f"exact-preplant forecast failed: {forecast_json}")
    roots, intent, plan = deterministic_unpublished_install_contract(fixture)
    document = core._initialization_intent(
        roots,
        intent,
        plan,
        fixture.stage_nonce,
    )
    stage_path = fixture.manifest.parent / "runtime-bootstrap-install-stage.json"
    stage_path.write_bytes(core.document_bytes(document))
    external_identity = core.node_identity(os.lstat(stage_path))
    if core.sha256_bytes(stage_path.read_bytes()) != fixture.expected_stage_sha256:
        raise AssertionError("exact-preplant bytes do not match the WhatIf external hash")
    refused, refused_json = stage_fixture(fixture)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or core.node_identity(os.lstat(stage_path)) != external_identity
        or core.document_bytes(document) != stage_path.read_bytes()
        or os.path.lexists(transaction_dir(fixture))
        or oni_tree_fingerprint(fixture) != oni_before
    ):
        raise AssertionError(f"first Stage adopted an exact preplant: {refused_json}")
    results.append(
        {
            "name": "stage_exact_preplant_refused_without_recovery_authority",
            "status": "passed",
            "classification": "stage_ownership_attack",
        }
    )
    return results


def run_abort_happy_path_and_replay(
    base: pathlib.Path, round_index: int
) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-abort-happy-replay")
    oni_before_install = oni_tree_fingerprint(fixture)
    clone_before = core.stable_file(
        fixture.clone_manifest,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(f"abort happy-path install failed: {installed_json}")
    assert_installed(fixture, installed_json)
    installed_runtime = runtime_fingerprint(fixture)

    before_what_if = optional_bound_tree_fingerprint(fixture.test_root)
    forecast, forecast_json = forecast_abort_stage(fixture)
    if (
        forecast.returncode != 0
        or forecast_json.get("status") != "would_abort_pre_config"
        or optional_bound_tree_fingerprint(fixture.test_root) != before_what_if
        or runtime_fingerprint(fixture) != installed_runtime
    ):
        raise AssertionError(f"abort WhatIf was not exact zero-write: {forecast_json}")
    staged, staged_json = stage_abort_fixture(fixture)
    if (
        staged.returncode != 0
        or staged_json.get("status") != "abort_staged"
        or staged_json.get("stage_sha256") != fixture.expected_abort_stage_sha256
        or staged_json.get("abort_id") != fixture.expected_abort_id
        or staged_json.get("runtime_write_count") != 0
        or runtime_fingerprint(fixture) != installed_runtime
    ):
        raise AssertionError(f"abort stage was not workspace-only: {staged_json}")
    aborted, aborted_json = invoke_abort(fixture, apply=True)
    if aborted.returncode != 0:
        raise AssertionError(f"pre-config abort failed: {aborted_json}")
    assert_aborted(
        fixture,
        aborted_json,
        expected_oni_fingerprint=oni_before_install,
        expected_clone_manifest_state=clone_before,
    )

    abort_root = transaction_dir(fixture) / "abort"
    immutable_paths = [
        abort_root / "result-candidate.json",
        abort_root / "result.json",
        *sorted((abort_root / "journal").glob("*.json")),
    ]
    immutable_hashes = {
        path.relative_to(abort_root).as_posix(): core.sha256_bytes(path.read_bytes())
        for path in immutable_paths
    }
    result_hash = json.loads(
        (abort_root / "result.json").read_text(encoding="utf-8")
    )["result_hash"]
    for replay_index in range(1, 4):
        replay, replay_json = invoke_abort(fixture, apply=True)
        if (
            replay.returncode != 0
            or replay_json.get("status") != "already_aborted"
            or replay_json.get("result_hash") != result_hash
            or replay_json.get("runtime_write_count") != 0
        ):
            raise AssertionError(f"completed abort replay {replay_index} diverged: {replay_json}")
        assert_aborted(
            fixture,
            replay_json,
            expected_oni_fingerprint=oni_before_install,
            expected_clone_manifest_state=clone_before,
        )
        current_hashes = {
            path.relative_to(abort_root).as_posix(): core.sha256_bytes(path.read_bytes())
            for path in immutable_paths
        }
        if current_hashes != immutable_hashes:
            raise AssertionError(
                f"completed abort replay {replay_index} rewrote immutable evidence"
            )
    return {
        "name": "preconfig_abort_exact_restore_and_three_replays",
        "status": "passed",
        "classification": "transactional_abort",
    }


def run_abort_stage_pre_return_recovery(
    base: pathlib.Path, round_index: int
) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-abort-stage-pre-return")
    oni_before_install = oni_tree_fingerprint(fixture)
    clone_before = core.stable_file(
        fixture.clone_manifest,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(installed_json)
    assert_installed(fixture, installed_json)
    installed_runtime = runtime_fingerprint(fixture)
    abort_support = transaction_dir(fixture) / "abort"
    abort_support_before = optional_bound_tree_fingerprint(abort_support)
    forecast, forecast_json = forecast_abort_stage(fixture)
    if forecast.returncode != 0:
        raise AssertionError(f"abort-stage forecast failed: {forecast_json}")
    interrupted, interrupted_json = stage_abort_fixture(
        fixture, fail_at="after_abort_stage_publish"
    )
    if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
        raise AssertionError(f"abort-stage publication did not interrupt: {interrupted_json}")
    stage_path = fixture.manifest.parent / "runtime-bootstrap-abort-stage.json"
    if (
        core.sha256_bytes(stage_path.read_bytes())
        != fixture.expected_abort_stage_sha256
        or optional_bound_tree_fingerprint(abort_support) != abort_support_before
        or runtime_fingerprint(fixture) != installed_runtime
    ):
        raise AssertionError("abort stage pre-return crash reached physical/support state")
    retried, retried_json = stage_abort_fixture(fixture, recover=True)
    if (
        retried.returncode != 0
        or retried_json.get("status") != "abort_stage_recovered"
        or retried_json.get("stage_sha256") != fixture.expected_abort_stage_sha256
        or retried_json.get("abort_id") != fixture.expected_abort_id
        or retried_json.get("runtime_write_count") != 0
    ):
        raise AssertionError(f"abort stage retry did not converge: {retried_json}")
    aborted, aborted_json = invoke_abort(fixture, apply=True)
    if aborted.returncode != 0:
        raise AssertionError(f"abort stage retry apply failed: {aborted_json}")
    assert_aborted(
        fixture,
        aborted_json,
        expected_oni_fingerprint=oni_before_install,
        expected_clone_manifest_state=clone_before,
    )
    return {
        "name": "abort_stage_pre_return_crash_external_retry",
        "status": "passed",
        "classification": "workspace_only_crash_recovery",
    }


def run_abort_exact_stage_preplant(
    base: pathlib.Path,
    round_index: int,
) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-abort-stage-exact-preplant")
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(installed_json)
    assert_installed(fixture, installed_json)
    installed_runtime = runtime_fingerprint(fixture)
    forecast, forecast_json = forecast_abort_stage(fixture)
    if forecast.returncode != 0:
        raise AssertionError(f"abort exact-preplant forecast failed: {forecast_json}")
    roots, intent, plan = deterministic_unpublished_abort_contract(fixture)
    document = core._abort_stage_document(
        roots,
        intent,
        plan,
        fixture.abort_stage_nonce,
    )
    stage_path = fixture.manifest.parent / "runtime-bootstrap-abort-stage.json"
    stage_path.write_bytes(core.document_bytes(document))
    external_identity = core.node_identity(os.lstat(stage_path))
    if core.sha256_bytes(stage_path.read_bytes()) != fixture.expected_abort_stage_sha256:
        raise AssertionError("abort exact-preplant bytes differ from WhatIf external hash")
    refused, refused_json = stage_abort_fixture(fixture)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or core.node_identity(os.lstat(stage_path)) != external_identity
        or core.document_bytes(document) != stage_path.read_bytes()
        or os.path.lexists(transaction_dir(fixture) / "abort" / "owner.json")
        or runtime_fingerprint(fixture) != installed_runtime
    ):
        raise AssertionError(f"first abort Stage adopted an exact preplant: {refused_json}")
    return {
        "name": "abort_stage_exact_preplant_refused_without_recovery_authority",
        "status": "passed",
        "classification": "stage_ownership_attack",
    }


def _replace_file_with_same_state(
    fixture: Fixture,
    path: pathlib.Path,
    label: str,
) -> pathlib.Path:
    before = os.lstat(path)
    parent_before = os.lstat(path.parent)
    payload = path.read_bytes()
    held_root = fixture.test_root / "attacker-held-explicit-files"
    held_root.mkdir(exist_ok=True)
    held = held_root / f"{label}-{path.name}"
    path.rename(held)
    path.write_bytes(payload)
    os.utime(path, ns=(int(before.st_atime_ns), int(before.st_mtime_ns)))
    os.utime(
        path.parent,
        ns=(int(parent_before.st_atime_ns), int(parent_before.st_mtime_ns)),
    )
    after = os.lstat(path)
    if (
        core.node_identity(before) == core.node_identity(after)
        or int(after.st_size) != int(before.st_size)
        or int(after.st_mtime_ns) != int(before.st_mtime_ns)
        or core.sha256_bytes(path.read_bytes()) != core.sha256_bytes(payload)
    ):
        raise AssertionError(f"{label} did not create an exact-content new inode")
    return held


def _replace_parent_with_same_tree(
    fixture: Fixture,
    parent: pathlib.Path,
    label: str,
) -> pathlib.Path:
    before = os.lstat(parent)
    held_root = fixture.test_root / "attacker-held-explicit-parents"
    held_root.mkdir(exist_ok=True)
    held = held_root / label
    parent.rename(held)
    shutil.copytree(held, parent, copy_function=shutil.copy2)
    os.utime(parent, ns=(int(before.st_atime_ns), int(before.st_mtime_ns)))
    after = os.lstat(parent)
    if core.node_identity(before) == core.node_identity(after):
        raise AssertionError(f"{label} did not replace the explicit parent inode")
    return held


def _assert_abort_prephysical_refusal(
    fixture: Fixture,
    installed_runtime: str,
    label: str,
) -> dict[str, Any]:
    refused, refused_json = invoke_abort(fixture, apply=True)
    if refused.returncode != 2 or refused_json.get("status") != "blocked":
        raise AssertionError(f"{label} was not blocked: {refused_json}")
    if runtime_fingerprint(fixture) != installed_runtime:
        raise AssertionError(f"{label} reached a Probe/Bridge/archive physical write")
    assert_no_abort_result(fixture, label)
    assert_no_live_environment_lock(fixture, label)
    latch = transaction_dir(fixture) / "abort" / "outcome-unknown.json"
    if os.path.lexists(latch) or os.path.lexists(latch.with_name(latch.name + ".new")):
        raise AssertionError(f"{label} incorrectly published a post-mutation latch")
    return {
        "name": label,
        "status": "passed",
        "classification": "prephysical_identity_attack",
    }


def run_abort_explicit_owner_attacks(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    file_roles: tuple[tuple[str, Callable[[Fixture], pathlib.Path]], ...] = (
        ("pre_boundary", lambda item: item.pre_boundary),
        ("clone_manifest", lambda item: item.clone_manifest),
        ("source_save", lambda item: item.source_save),
        ("source_preview", lambda item: item.source_save.with_suffix(".png")),
        ("loader_save", lambda item: item.loader_save),
        ("loader_preview", lambda item: item.loader_save.with_suffix(".png")),
    )
    for role, resolve in file_roles:
        fixture = make_fixture(base, f"r{round_index}-abort-swap-{role}")
        _before, _clone, installed_runtime = prepare_installed_staged_abort(fixture)
        held = _replace_file_with_same_state(fixture, resolve(fixture), role)
        results.append(
            _assert_abort_prephysical_refusal(
                fixture,
                installed_runtime,
                f"abort_explicit_same_bytes_inode_swap_refused:{role}",
            )
        )
        if not held.exists():
            raise AssertionError(f"{role} attack did not preserve the external original")

    parent_roles: tuple[tuple[str, Callable[[Fixture], pathlib.Path]], ...] = (
        ("pre_boundary_parent", lambda item: item.pre_boundary.parent),
        ("clone_manifest_parent", lambda item: item.clone_manifest.parent),
        ("save_parent", lambda item: item.loader_save.parent),
    )
    for role, resolve in parent_roles:
        fixture = make_fixture(base, f"r{round_index}-abort-parent-{role}")
        _before, _clone, installed_runtime = prepare_installed_staged_abort(fixture)
        held = _replace_parent_with_same_tree(fixture, resolve(fixture), role)
        results.append(
            _assert_abort_prephysical_refusal(
                fixture,
                installed_runtime,
                f"abort_explicit_parent_inode_swap_refused:{role}",
            )
        )
        if not held.exists():
            raise AssertionError(f"{role} attack did not preserve the external original")
    return results


def run_abort_save_inventory_attacks(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    fixture = make_fixture(base, f"r{round_index}-abort-save-extra")
    _before, _clone, installed_runtime = prepare_installed_staged_abort(fixture)
    extra = fixture.oni / "cloud_save_files" / "unexpected-second-run.sav"
    extra.write_bytes(b"external-save")
    results.append(
        _assert_abort_prephysical_refusal(
            fixture,
            installed_runtime,
            "abort_extra_save_inventory_entry_refused",
        )
    )
    if extra.read_bytes() != b"external-save":
        raise AssertionError("abort extra-save refusal changed the external file")

    fixture = make_fixture(base, f"r{round_index}-abort-save-second-loader")
    _before, _clone, installed_runtime = prepare_installed_staged_abort(fixture)
    second_save = fixture.loader_save.with_name(f"{fixture.loader_stem}-other.sav")
    second_preview = second_save.with_suffix(".png")
    shutil.copy2(fixture.source_save, second_save)
    shutil.copy2(fixture.source_save.with_suffix(".png"), second_preview)
    results.append(
        _assert_abort_prephysical_refusal(
            fixture,
            installed_runtime,
            "abort_second_named_loader_pair_refused",
        )
    )

    fixture = make_fixture(base, f"r{round_index}-abort-save-missing")
    _before, _clone, installed_runtime = prepare_installed_staged_abort(fixture)
    missing = fixture.oni / "save_files" / "FixtureColony" / "AutoSave-001.sav"
    held = fixture.test_root / "attacker-held-autosave.sav"
    missing.rename(held)
    results.append(
        _assert_abort_prephysical_refusal(
            fixture,
            installed_runtime,
            "abort_missing_preboundary_autosave_refused",
        )
    )
    if not held.is_file():
        raise AssertionError("missing-autosave attack did not preserve the external original")

    fixture = make_fixture(base, f"r{round_index}-abort-save-mtime")
    _before, _clone, installed_runtime = prepare_installed_staged_abort(fixture)
    drift = fixture.oni / "cloud_save_files" / "CloudFixture.png"
    metadata = os.lstat(drift)
    os.utime(
        drift,
        ns=(int(metadata.st_atime_ns), int(metadata.st_mtime_ns) + 10_000_000),
    )
    results.append(
        _assert_abort_prephysical_refusal(
            fixture,
            installed_runtime,
            "abort_save_mtime_only_drift_refused",
        )
    )
    return results


def run_abort_preconfig_gate_attacks(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    artifacts: tuple[tuple[str, Callable[[Fixture], pathlib.Path]], ...] = (
        (
            "runtime_config_evidence",
            lambda item: item.manifest.parent / "runtime-config-evidence.json",
        ),
        (
            "runtime_startup_binding",
            lambda item: item.manifest.parent / "runtime-startup-binding.json",
        ),
        (
            "runtime_config_recovery",
            lambda item: item.manifest.parent / "runtime-config-recovery-tx-test.json",
        ),
        (
            "runtime_config_stage_claim",
            lambda item: item.oni / "mods" / ".g0-config-stage-deadbeef.tmp",
        ),
        (
            "startup_consumption_evidence",
            lambda item: item.evidence
            / item.run_id
            / "startup-consumption-evidence"
            / "evidence.json",
        ),
        (
            "priority_action_evidence",
            lambda item: item.evidence
            / item.run_id
            / "confirmed-command-evidence"
            / "command"
            / "approval-bundle.json",
        ),
        (
            "environment_backup_hidden_transaction",
            lambda item: item.manifest.parent
            / "backup"
            / "runtime-config-partial-claim.json",
        ),
    )
    for label, resolve in artifacts:
        label_token = core.sha256_bytes(label.encode("utf-8")).removeprefix("sha256:")[:10]
        fixture = make_fixture(base, f"r{round_index}-abort-gate-{label_token}")
        installed, installed_json = invoke(fixture, apply=True)
        if installed.returncode != 0:
            raise AssertionError(f"{label} install failed: {installed_json}")
        assert_installed(fixture, installed_json)
        artifact = resolve(fixture)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b'{"external":"preconfig-evidence"}\n')
        runtime_after_attack = runtime_fingerprint(fixture)
        refused, refused_json = forecast_abort_stage(fixture)
        if refused.returncode != 2 or refused_json.get("status") != "blocked":
            raise AssertionError(f"pre-config artifact {label} was accepted: {refused_json}")
        if runtime_fingerprint(fixture) != runtime_after_attack:
            raise AssertionError(f"pre-config artifact {label} reached a physical write")
        if artifact.read_bytes() != b'{"external":"preconfig-evidence"}\n':
            raise AssertionError(f"pre-config artifact {label} was changed")
        assert_no_abort_result(fixture, label)
        results.append(
            {
                "name": f"abort_preconfig_artifact_refused:{label}",
                "status": "passed",
                "classification": "preconfig_evidence_gate",
            }
        )

    fixture = make_fixture(base, f"r{round_index}-abort-process-running")
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(installed_json)
    assert_installed(fixture, installed_json)
    installed_runtime = runtime_fingerprint(fixture)
    write_json(fixture.process_fixture, {"OxygenNotIncluded": 1, "oni_bridge.py": 0})
    refused, refused_json = forecast_abort_stage(fixture)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or runtime_fingerprint(fixture) != installed_runtime
    ):
        raise AssertionError(f"running-process abort gate failed: {refused_json}")
    results.append(
        {
            "name": "abort_running_process_refused_before_stage",
            "status": "passed",
            "classification": "live_process_gate",
        }
    )
    return results


def run_abort_requires_installed_ready_cases(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    fixture = make_fixture(base, f"r{round_index}-abort-no-install")
    fixture.install_result_sha256 = "sha256:" + "0" * 64
    before = optional_bound_tree_fingerprint(fixture.test_root)
    refused, refused_json = forecast_abort_stage(fixture)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or optional_bound_tree_fingerprint(fixture.test_root) != before
        or os.path.lexists(transaction_dir(fixture))
    ):
        raise AssertionError(f"abort without install latch was not zero-write: {refused_json}")
    results.append(
        {
            "name": "abort_requires_install_transaction_and_ready_result",
            "status": "passed",
            "classification": "installed_ready_gate",
        }
    )

    for label, point in (
        ("partial_physical_install", "after_old_bridge_physical"),
        ("candidate_only_install", "after_result_candidate_publish"),
    ):
        fixture = make_fixture(base, f"r{round_index}-abort-unready-{label}")
        interrupted, interrupted_json = invoke(fixture, apply=True, fail_at=point)
        if interrupted.returncode != 99:
            raise AssertionError(f"{label} did not reach its install fault: {interrupted_json}")
        runtime_before_abort = runtime_fingerprint(fixture)
        fixture.install_result_sha256 = "sha256:" + "0" * 64
        refused, refused_json = forecast_abort_stage(fixture)
        if (
            refused.returncode != 2
            or refused_json.get("status") != "blocked"
            or runtime_fingerprint(fixture) != runtime_before_abort
            or os.path.lexists(
                fixture.manifest.parent / "runtime-bootstrap-abort-stage.json"
            )
        ):
            raise AssertionError(f"abort accepted {label}: {refused_json}")
        fixture.install_result_sha256 = None
        recovered, recovered_json = invoke(fixture, apply=True)
        if recovered.returncode != 0:
            raise AssertionError(f"install did not recover after {label}: {recovered_json}")
        assert_installed(fixture, recovered_json)
        results.append(
            {
                "name": f"abort_refuses_{label}_until_install_recovers_ready",
                "status": "passed",
                "classification": "installed_ready_gate",
            }
        )
    return results


def run_abort_anchor_attacks(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    fixture = make_fixture(base, f"r{round_index}-abort-wrong-install-result")
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(installed_json)
    assert_installed(fixture, installed_json)
    installed_runtime = runtime_fingerprint(fixture)
    fixture.install_result_sha256 = "sha256:" + "f" * 64
    refused, refused_json = forecast_abort_stage(fixture)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or runtime_fingerprint(fixture) != installed_runtime
    ):
        raise AssertionError(f"wrong install-result anchor was accepted: {refused_json}")
    results.append(
        {
            "name": "abort_wrong_install_result_external_hash_refused",
            "status": "passed",
            "classification": "external_anchor_attack",
        }
    )

    for label, mutate in (
        (
            "abort_stage_sha256",
            lambda item: setattr(
                item,
                "expected_abort_stage_sha256",
                "sha256:" + "f" * 64,
            ),
        ),
        (
            "abort_id",
            lambda item: setattr(item, "expected_abort_id", "rtb-abort-forged"),
        ),
    ):
        fixture = make_fixture(base, f"r{round_index}-abort-wrong-{label}")
        installed, installed_json = invoke(fixture, apply=True)
        if installed.returncode != 0:
            raise AssertionError(installed_json)
        assert_installed(fixture, installed_json)
        installed_runtime = runtime_fingerprint(fixture)
        forecast, forecast_json = forecast_abort_stage(fixture)
        if forecast.returncode != 0:
            raise AssertionError(f"{label} forecast failed: {forecast_json}")
        mutate(fixture)
        refused, refused_json = stage_abort_fixture(fixture)
        if (
            refused.returncode != 2
            or refused_json.get("status") != "blocked"
            or os.path.lexists(
                fixture.manifest.parent / "runtime-bootstrap-abort-stage.json"
            )
            or runtime_fingerprint(fixture) != installed_runtime
        ):
            raise AssertionError(f"wrong {label} was accepted: {refused_json}")
        results.append(
            {
                "name": f"abort_wrong_{label}_refused_before_stage",
                "status": "passed",
                "classification": "external_anchor_attack",
            }
        )

    fixture = make_fixture(base, f"r{round_index}-abort-wrong-loader-stem")
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(installed_json)
    assert_installed(fixture, installed_json)
    installed_runtime = runtime_fingerprint(fixture)
    fixture.loader_stem += "-wrong"
    refused, refused_json = forecast_abort_stage(fixture)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or runtime_fingerprint(fixture) != installed_runtime
    ):
        raise AssertionError(f"wrong loader stem was accepted: {refused_json}")
    results.append(
        {
            "name": "abort_wrong_loader_stem_refused",
            "status": "passed",
            "classification": "path_binding_attack",
        }
    )
    return results


def run_abort_fault_case(
    base: pathlib.Path,
    point: str,
    round_index: int,
    *,
    archive_present: bool = False,
) -> dict[str, Any]:
    point_token = core.sha256_bytes(point.encode("utf-8")).removeprefix("sha256:")[:12]
    fixture = make_fixture(
        base,
        f"r{round_index}-abort-fault-{point_token}",
        archive_present=archive_present,
    )
    oni_before_install = oni_tree_fingerprint(fixture)
    clone_before = core.stable_file(
        fixture.clone_manifest,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    installed, installed_json = invoke(fixture, apply=True)
    if installed.returncode != 0:
        raise AssertionError(
            f"abort fault {point} could not install its TestRoot runtime: {installed_json}"
        )
    assert_installed(fixture, installed_json)
    for mtime_role in (
        "probe_parent",
        "probe_container",
        "oni_root",
    ):
        if f"abort_{mtime_role}_mtime_restored" in point:
            force_abort_mtime_restore_path(fixture, mtime_role)
            break
    forecast, forecast_json = forecast_abort_stage(fixture)
    if forecast.returncode != 0:
        raise AssertionError(f"abort fault {point} WhatIf failed: {forecast_json}")
    staged, staged_json = stage_abort_fixture(fixture)
    if staged.returncode != 0:
        raise AssertionError(f"abort fault {point} stage failed: {staged_json}")
    interrupted, interrupted_json = invoke_abort(
        fixture,
        apply=True,
        fail_at=point,
    )
    if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
        raise AssertionError(
            f"abort fault {point} did not stop at its exact checkpoint: "
            f"{interrupted_json}"
        )
    latch = transaction_dir(fixture) / "abort" / "outcome-unknown.json"
    if os.path.lexists(latch) or os.path.lexists(latch.with_name(latch.name + ".new")):
        raise AssertionError(
            f"simulated power loss {point} was incorrectly converted to outcome_unknown"
        )
    recovered, recovered_json = invoke_abort(fixture, apply=True)
    if recovered.returncode != 0:
        raise AssertionError(f"abort fault {point} did not recover: {recovered_json}")
    assert_aborted(
        fixture,
        recovered_json,
        expected_oni_fingerprint=oni_before_install,
        expected_clone_manifest_state=clone_before,
    )
    return {
        "name": f"abort_fault_recovery:{point}",
        "status": "passed",
        "classification": "transactional_abort_crash_recovery",
    }


def run_abort_attempt_parent_claim_attacks(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    """Only an exact final parent claim may authorize filling owner.json."""

    results: list[dict[str, Any]] = []
    before_claim = "after_environment_lock_attempt_dir_physical_before_parent_claim"
    after_claim = "after_environment_lock_attempt_parent_claim"
    for variant, point in (
        ("missing_claim", before_claim),
        ("partial_claim", before_claim),
        ("owner_partial_without_claim", before_claim),
        ("owner_complete_without_claim", before_claim),
        ("directory_identity_swap", after_claim),
        ("unexpected_child", after_claim),
    ):
        token = core.sha256_bytes(
            f"attempt-parent:{variant}".encode("utf-8")
        ).removeprefix("sha256:")[:12]
        fixture = make_fixture(base, f"r{round_index}-abort-attempt-{token}")
        _oni_before, _clone_before, installed_runtime = prepare_installed_staged_abort(
            fixture
        )
        interrupted, interrupted_json = invoke_abort(
            fixture,
            apply=True,
            fail_at=point,
        )
        if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
            raise AssertionError(
                f"abort attempt-parent {variant} did not reach its exact crash window: "
                f"{interrupted_json}"
            )
        attempt_root = transaction_dir(fixture) / "environment-lock-attempts"
        attempts = sorted(
            (
                path
                for path in attempt_root.iterdir()
                if path.is_dir() and path.name.endswith("-abort")
            ),
            key=lambda path: path.name,
        )
        if len(attempts) != 1:
            raise AssertionError(
                f"abort attempt-parent {variant} has an unexpected abort epoch set"
            )
        attempt = attempts[0]
        claim_path = attempt_root / f"{attempt.name}.claim.json"
        partial_claim_path = claim_path.with_name(claim_path.name + ".new")
        if variant in {
            "missing_claim",
            "partial_claim",
            "owner_partial_without_claim",
            "owner_complete_without_claim",
        }:
            if os.path.lexists(claim_path) or any(attempt.iterdir()):
                raise AssertionError(
                    f"abort attempt-parent {variant} crossed the pre-claim boundary"
                )
            if variant == "partial_claim":
                partial_claim_path.write_bytes(b'{"partial":"external-crash-prefix"')
            elif variant in {
                "owner_partial_without_claim",
                "owner_complete_without_claim",
            }:
                install_plan, _ = core.stable_json(
                    transaction_dir(fixture) / "plan.json",
                    "abort attempt-parent install plan",
                )
                match = re.fullmatch(r"(\d{4})-abort", attempt.name)
                if match is None:
                    raise AssertionError(
                        "abort owner-without-claim fixture has an invalid epoch"
                    )
                owner = core._lock_attempt_owner_contract(
                    install_plan,
                    int(match.group(1)),
                    "abort",
                    core.node_identity(os.lstat(attempt)),
                )
                payload = core.document_bytes(owner)
                if variant == "owner_partial_without_claim":
                    (attempt / "owner.json.new").write_bytes(
                        payload[: max(1, len(payload) // 2)]
                    )
                else:
                    (attempt / "owner.json").write_bytes(payload)
        else:
            claim, _ = core.stable_json(
                claim_path,
                "abort environment-lock final parent claim",
            )
            if (
                claim.get("directory_identity") != core.node_identity(os.lstat(attempt))
                or any(attempt.iterdir())
            ):
                raise AssertionError(
                    f"abort attempt-parent {variant} lacks its exact empty claimed inode"
                )
            if variant == "directory_identity_swap":
                held = attempt.with_name(attempt.name + "-externally-held")
                attempt.rename(held)
                attempt.mkdir()
            else:
                (attempt / "external-unexpected-child.bin").write_bytes(b"external")
        attempt_state = optional_bound_tree_fingerprint(attempt_root)
        runtime_before_retry = runtime_fingerprint(fixture)
        if runtime_before_retry != installed_runtime:
            raise AssertionError(
                f"abort attempt-parent {variant} reached physical runtime state"
            )
        retried, retried_json = invoke_abort(fixture, apply=True)
        if (
            retried.returncode != 2
            or retried_json.get("status") != "blocked"
            or optional_bound_tree_fingerprint(attempt_root) != attempt_state
            or runtime_fingerprint(fixture) != installed_runtime
        ):
            raise AssertionError(
                f"abort attempt-parent {variant} was adopted or replayed: {retried_json}"
            )
        results.append(
            _assert_abort_outcome_unknown_latch(
                fixture,
                f"abort_attempt_parent_{variant}_manual_latch",
            )
        )
    return results


def run_abort_transformed_archive_custody_cases(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    """Exercise the step2-root/step3-child physical-before-journal window."""

    results: list[dict[str, Any]] = []
    point = "after_abort_old_bridge_restored_physical"
    for variant in (
        "wrong_live_bridge",
        "duplicate_relocated_source",
        "unexpected_archive_remainder",
        "archive_root_identity_swap",
    ):
        token = core.sha256_bytes(
            f"abort-transformed-archive:{variant}".encode("utf-8")
        ).removeprefix("sha256:")[:12]
        fixture = make_fixture(base, f"r{round_index}-abort-transform-{token}")
        prepare_installed_staged_abort(fixture)
        interrupted, interrupted_json = invoke_abort(
            fixture,
            apply=True,
            fail_at=point,
        )
        if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
            raise AssertionError(
                f"abort transformed-custody {variant} missed its crash window: "
                f"{interrupted_json}"
            )
        latch_path = transaction_dir(fixture) / "abort" / "outcome-unknown.json"
        if os.path.lexists(latch_path) or os.path.lexists(
            latch_path.with_name(latch_path.name + ".new")
        ):
            raise AssertionError(
                f"abort transformed-custody {variant} crash published a latch"
            )
        parsed = parse_abort_apply_args(fixture)
        roots = core.resolve_roots(parsed)
        stage, _ = core.stable_json(
            fixture.manifest.parent / "runtime-bootstrap-abort-stage.json",
            "abort transformed-custody stage",
        )
        abort_plan = stage.get("abort_plan")
        if not isinstance(abort_plan, Mapping):
            raise AssertionError("abort transformed-custody stage has no plan")
        explicit = {
            "pre_boundary": core.normalized(parsed.pre_boundary),
            "clone_manifest": core.normalized(parsed.clone_manifest),
            "source_save": core.normalized(parsed.source_save),
            "loader_save": core.normalized(parsed.loader_save),
        }
        explicit["source_preview"] = explicit["source_save"].with_suffix(".png")
        explicit["loader_preview"] = explicit["loader_save"].with_suffix(".png")
        roles = core._abort_role_paths(roots, abort_plan, explicit)
        steps = {
            str(step.get("event")): step for step in abort_plan.get("steps", [])
        }
        root_step = steps.get("created_archive_root_archived")
        bridge_step = steps.get("old_bridge_restored")
        if not isinstance(root_step, Mapping) or not isinstance(bridge_step, Mapping):
            raise AssertionError("abort transformed-custody fixture lacks its two steps")
        broad_target = roles["abort_created_archive_root"]
        relocated_source = roles["relocated_old_bridge_archive"]
        live_bridge = roles["live_bridge"]
        transformed_snapshot, transformed_identity = core.capture_bound_tree(
            broad_target
        )
        if (
            transformed_snapshot.get("entries") != []
            or not core.exact_json_equal(
                transformed_identity.get("root_identity"),
                root_step.get("owner", {}).get("identity", {}).get(
                    "root_identity"
                ),
            )
            or os.path.lexists(relocated_source)
            or not core._abort_owner_matches(live_bridge, bridge_step["owner"])
        ):
            raise AssertionError(
                f"abort transformed-custody {variant} has the wrong baseline"
            )
        journal_events = {
            str(entry.get("event"))
            for entry in core._verify_abort_journal(
                transaction_dir(fixture) / "abort", abort_plan
            )
        }
        if (
            "created_archive_root_archived" not in journal_events
            or "old_bridge_restored" in journal_events
        ):
            raise AssertionError(
                f"abort transformed-custody {variant} is not in the journal gap"
            )

        held = fixture.test_root / f"externally-held-{variant}"
        if variant == "wrong_live_bridge":
            live_bridge.rename(held)
            live_bridge.mkdir()
            (live_bridge / "external-wrong-live.bin").write_bytes(b"external-live")
        elif variant == "duplicate_relocated_source":
            relocated_source.mkdir()
            (relocated_source / ABORT_RELOCATED_SOURCE_MARKER).write_bytes(
                b"external-source"
            )
        elif variant == "unexpected_archive_remainder":
            (broad_target / "external-remainder.bin").write_bytes(
                b"external-remainder"
            )
        else:
            broad_target.rename(held)
            broad_target.mkdir()

        protected_paths = [broad_target, relocated_source, live_bridge]
        if os.path.lexists(held):
            protected_paths.append(held)
        protected_before = core.canonical_hash(
            {
                core.path_id(path): optional_bound_tree_fingerprint(path)
                for path in protected_paths
            }
        )
        runtime_before = runtime_fingerprint(fixture)
        refused, refused_json = invoke_abort(fixture, apply=True)
        protected_after = core.canonical_hash(
            {
                core.path_id(path): optional_bound_tree_fingerprint(path)
                for path in protected_paths
            }
        )
        if (
            refused.returncode != 2
            or refused_json.get("status") != "blocked"
            or protected_after != protected_before
            or runtime_fingerprint(fixture) != runtime_before
        ):
            raise AssertionError(
                f"abort transformed-custody {variant} was mutated/replayed: "
                f"{refused_json}"
            )
        results.append(
            _assert_abort_outcome_unknown_latch(
                fixture,
                f"abort_transformed_archive_{variant}_manual_latch",
                allow_owned_live_lock=True,
            )
        )
    return results


def run_abort_ready_lock_fault_case(
    base: pathlib.Path,
    point: str,
    round_index: int,
) -> dict[str, Any]:
    point_token = core.sha256_bytes(
        ("ready:" + point).encode("utf-8")
    ).removeprefix("sha256:")[:12]
    fixture = make_fixture(base, f"r{round_index}-abort-ready-{point_token}")
    oni_before_install, clone_before, _runtime = prepare_installed_staged_abort(fixture)
    occurrence = 0

    def second_epoch_hook(observed: str) -> None:
        nonlocal occurrence
        if observed != point:
            return
        occurrence += 1
        if occurrence == 2:
            raise core.InjectedCrash(f"second-occurrence:{point}")

    try:
        core.execute_abort(
            parse_abort_apply_args(fixture),
            hook=second_epoch_hook,
        )
    except core.InjectedCrash:
        pass
    else:
        raise AssertionError(f"abort ready-lock point {point} did not interrupt")
    if occurrence != 2:
        raise AssertionError(
            f"abort ready-lock point {point} observed {occurrence} occurrences"
        )
    latch = transaction_dir(fixture) / "abort" / "outcome-unknown.json"
    if os.path.lexists(latch) or os.path.lexists(latch.with_name(latch.name + ".new")):
        raise AssertionError(f"ready-lock crash {point} published outcome_unknown")
    recovered, recovered_json = invoke_abort(fixture, apply=True)
    if recovered.returncode != 0:
        raise AssertionError(
            f"abort ready-lock crash {point} did not recover: {recovered_json}"
        )
    assert_aborted(
        fixture,
        recovered_json,
        expected_oni_fingerprint=oni_before_install,
        expected_clone_manifest_state=clone_before,
    )
    return {
        "name": f"abort_ready_lock_fault_recovery:{point}",
        "status": "passed",
        "classification": "ready_publication_crash_recovery",
    }


def _assert_abort_outcome_unknown_latch(
    fixture: Fixture,
    label: str,
    *,
    allow_owned_live_lock: bool = False,
    allow_preserved_external_live_lock: bool = False,
) -> dict[str, Any]:
    latch_path = transaction_dir(fixture) / "abort" / "outcome-unknown.json"
    latch, _state = core.stable_json(
        latch_path,
        "pre-config abort outcome-unknown latch",
    )
    claimed = latch.get("latch_hash")
    unsigned = dict(latch)
    unsigned.pop("latch_hash", None)
    exact_keys = {
        "schema",
        "status",
        "classification",
        "run_id",
        "abort_id",
        "abort_plan_hash",
        "abort_stage_sha256",
        "abort_stage_hash",
        "journal",
        "role_locations",
        "failure_type",
        "failure_hash",
        "manual_recovery_required",
        "game_launched",
        "latch_hash",
    }
    stage, stage_state = core.stable_json(
        fixture.manifest.parent / "runtime-bootstrap-abort-stage.json",
        "pre-config abort external stage",
    )
    abort_plan = stage.get("abort_plan")
    parsed = parse_abort_apply_args(fixture)
    roots = core.resolve_roots(parsed)
    explicit = {
        "pre_boundary": core.normalized(parsed.pre_boundary),
        "clone_manifest": core.normalized(parsed.clone_manifest),
        "source_save": core.normalized(parsed.source_save),
        "loader_save": core.normalized(parsed.loader_save),
    }
    explicit["source_preview"] = explicit["source_save"].with_suffix(".png")
    explicit["loader_preview"] = explicit["loader_save"].with_suffix(".png")
    roles = (
        core._abort_role_paths(roots, abort_plan, explicit)
        if isinstance(abort_plan, Mapping)
        else {}
    )
    role_locations = latch.get("role_locations")
    journal = latch.get("journal")
    if (
        set(latch) != exact_keys
        or latch.get("schema")
        != "oni.live-runtime-bootstrap-abort-outcome-unknown.v1"
        or latch.get("status") != "outcome_unknown"
        or latch.get("classification") != "infra_fail"
        or latch.get("run_id") != fixture.run_id
        or not isinstance(abort_plan, Mapping)
        or latch.get("abort_id") != abort_plan.get("abort_id")
        or latch.get("abort_plan_hash") != abort_plan.get("plan_hash")
        or latch.get("abort_stage_sha256") != stage_state.get("sha256")
        or latch.get("abort_stage_hash") != stage.get("stage_hash")
        or not isinstance(journal, Mapping)
        or set(journal) != {"entry_count", "tip_hash", "status"}
        or journal.get("status") not in {"verified", "unverified"}
        or (
            journal.get("status") == "unverified"
            and (journal.get("entry_count") is not None or journal.get("tip_hash") is not None)
        )
        or not isinstance(role_locations, Mapping)
        or set(role_locations) != set(roles)
        or any(
            not isinstance(role_locations.get(role), Mapping)
            or set(role_locations[role]) != {"path_id_lower", "state"}
            or role_locations[role].get("path_id_lower") != core.path_id(path)
            or not core.exact_json_equal(
                role_locations[role].get("state"),
                core._abort_role_location(path),
            )
            for role, path in roles.items()
        )
        or not isinstance(latch.get("failure_type"), str)
        or core.HASH_RE.fullmatch(str(latch.get("failure_hash") or "")) is None
        or latch.get("manual_recovery_required") is not True
        or latch.get("game_launched") is not False
        or claimed != core.canonical_hash(unsigned)
    ):
        raise AssertionError(f"{label} published an invalid recovery latch: {latch}")
    before_retry = optional_bound_tree_fingerprint(fixture.test_root)
    refused, refused_json = invoke_abort(fixture, apply=True)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or "outcome_unknown" not in str(refused_json).casefold()
        or optional_bound_tree_fingerprint(fixture.test_root) != before_retry
    ):
        raise AssertionError(f"{label} latch did not prohibit physical replay: {refused_json}")
    assert_no_abort_result(fixture, label)
    if allow_owned_live_lock and allow_preserved_external_live_lock:
        raise AssertionError(f"{label} selected two incompatible live-lock modes")
    if allow_owned_live_lock:
        install_plan, _ = core.stable_json(
            transaction_dir(fixture) / "plan.json",
            "latched abort install plan",
        )
        attempt_root = transaction_dir(fixture) / "environment-lock-attempts"
        attempts = core._lock_attempt_directories(attempt_root, install_plan)
        if not attempts:
            raise AssertionError(f"{label} has no owned abort lock epoch")
        live = fixture.manifest.parent / ".finalize.lock"
        live_state = core.stable_file(
            live, max_bytes=core.MAX_JSON_BYTES, collect=True
        )
        matching_owned_epochs = []
        for attempt in attempts:
            state = core._load_lock_attempt(roots, install_plan, attempt)
            claim = state.get("claim")
            if (
                state.get("purpose") == "abort"
                and state.get("released") is not True
                and state.get("release_complete") is not True
                and isinstance(claim, Mapping)
                and live_state.get("identity") == claim.get("lock_identity")
                and live_state.get("sha256") == claim.get("payload_sha256")
            ):
                matching_owned_epochs.append(attempt.name)
        if len(matching_owned_epochs) != 1:
            raise AssertionError(
                f"{label} live lock is not its exact unreleased abort custody"
            )
    elif allow_preserved_external_live_lock:
        live = fixture.manifest.parent / ".finalize.lock"
        if not os.path.lexists(live):
            raise AssertionError(f"{label} lost its preserved external live lock")
    else:
        assert_no_live_environment_lock(fixture, label)
    return {
        "name": label,
        "status": "passed",
        "classification": "outcome_unknown_manual_recovery",
    }


def run_abort_process_toctou_cases(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    fixture = make_fixture(base, f"r{round_index}-abort-process-first-window")
    oni_before, clone_before, installed_runtime = prepare_installed_staged_abort(fixture)
    fired = False

    def first_step_process_start(point: str) -> None:
        nonlocal fired
        if point == "before_abort_runtime_bridge_archived_physical" and not fired:
            fired = True
            write_json(
                fixture.process_fixture,
                {"OxygenNotIncluded": 1, "oni_bridge.py": 0},
            )

    try:
        core.execute_abort(
            parse_abort_apply_args(fixture),
            hook=first_step_process_start,
        )
    except core.BootstrapError:
        pass
    else:
        raise AssertionError("abort first-step process TOCTOU was accepted")
    if (
        not fired
        or runtime_fingerprint(fixture) != installed_runtime
        or core._abort_latch_present(core.resolve_roots(parse_abort_apply_args(fixture)))
    ):
        raise AssertionError("abort first-step process race reached mutation/latch state")
    write_json(fixture.process_fixture, {"OxygenNotIncluded": 0, "oni_bridge.py": 0})
    recovered, recovered_json = invoke_abort(fixture, apply=True)
    if recovered.returncode != 0:
        raise AssertionError(f"abort first-step process recovery failed: {recovered_json}")
    assert_aborted(
        fixture,
        recovered_json,
        expected_oni_fingerprint=oni_before,
        expected_clone_manifest_state=clone_before,
    )
    results.append(
        {
            "name": "abort_process_start_in_first_physical_window_zero_write",
            "status": "passed",
            "classification": "live_process_toctou",
        }
    )

    fixture = make_fixture(base, f"r{round_index}-abort-process-late-window")
    _before, _clone, _runtime = prepare_installed_staged_abort(fixture)
    fired = False

    def late_process_start(point: str) -> None:
        nonlocal fired
        if point == "before_abort_probe_parent_mtime_restored_physical" and not fired:
            fired = True
            write_json(
                fixture.process_fixture,
                {"OxygenNotIncluded": 1, "oni_bridge.py": 0},
            )

    try:
        core.execute_abort(
            parse_abort_apply_args(fixture),
            hook=late_process_start,
        )
    except core.BootstrapError:
        pass
    else:
        raise AssertionError("abort late process TOCTOU was accepted")
    if not fired:
        raise AssertionError("abort late process TOCTOU hook was not reached")
    write_json(fixture.process_fixture, {"OxygenNotIncluded": 0, "oni_bridge.py": 0})
    results.append(
        _assert_abort_outcome_unknown_latch(
            fixture,
            "abort_process_start_after_partial_restore_latched",
            allow_owned_live_lock=True,
        )
    )
    return results


def run_abort_unfinished_lock_classification_cases(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    """Malformed abort lock custody must latch; exact latest custody resumes.

    The ordinary abort fault matrix covers the recoverable latest stage/live/
    acquired prefixes.  These cases construct the adjacent states that must
    never be normalized: two simultaneous names, a same-byte replacement
    inode, acquired evidence without its live inode, and a non-latest
    unfinished epoch.
    """

    results: list[dict[str, Any]] = []

    def file_observation(path: pathlib.Path) -> dict[str, Any]:
        if not os.path.lexists(path):
            return {"exists": False}
        return {
            "exists": True,
            "state": core.stable_file(
                path,
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
        }

    for variant, point in (
        ("two_locations", "after_environment_lock_publish"),
        ("live_same_bytes_new_inode", "after_environment_lock_acquired"),
        ("acquired_without_live", "after_environment_lock_acquired"),
        ("nonlatest_unfinished", "after_environment_lock_acquired"),
    ):
        token = core.sha256_bytes(
            f"abort-unfinished-lock:{variant}".encode("utf-8")
        ).removeprefix("sha256:")[:12]
        fixture = make_fixture(base, f"r{round_index}-abort-lock-{token}")
        _oni_before, _clone_before, installed_runtime = prepare_installed_staged_abort(
            fixture
        )
        interrupted, interrupted_json = invoke_abort(
            fixture,
            apply=True,
            fail_at=point,
        )
        if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
            raise AssertionError(
                f"abort unfinished-lock {variant} missed {point}: {interrupted_json}"
            )
        latch_path = transaction_dir(fixture) / "abort" / "outcome-unknown.json"
        if os.path.lexists(latch_path) or os.path.lexists(
            latch_path.with_name(latch_path.name + ".new")
        ):
            raise AssertionError(
                f"abort unfinished-lock {variant} crash published a latch"
            )

        parsed = parse_abort_apply_args(fixture)
        roots = core.resolve_roots(parsed)
        install_plan, _ = core.stable_json(
            transaction_dir(fixture) / "plan.json",
            "abort unfinished-lock install plan",
        )
        attempt_root = transaction_dir(fixture) / "environment-lock-attempts"
        attempts = core._lock_attempt_directories(attempt_root, install_plan)
        if not attempts or not attempts[-1].name.endswith("-abort"):
            raise AssertionError(
                f"abort unfinished-lock {variant} has no latest abort epoch"
            )
        attempt = attempts[-1]
        live = fixture.manifest.parent / ".finalize.lock"
        held: Optional[pathlib.Path] = None
        live_mode = "owned"

        if variant == "two_locations":
            live_payload = core.stable_file(
                live, max_bytes=core.MAX_JSON_BYTES, collect=True
            ).get("bytes")
            if not isinstance(live_payload, bytes):
                raise AssertionError("two-location fixture has no exact live payload")
            (attempt / "lock.stage").write_bytes(live_payload)
        elif variant in {"live_same_bytes_new_inode", "acquired_without_live"}:
            live_payload = core.stable_file(
                live, max_bytes=core.MAX_JSON_BYTES, collect=True
            ).get("bytes")
            if not isinstance(live_payload, bytes):
                raise AssertionError(f"{variant} fixture has no exact live payload")
            held = fixture.test_root / f"external-held-{token}.lock"
            live.rename(held)
            if variant == "live_same_bytes_new_inode":
                live.write_bytes(live_payload)
                held_identity = core.node_identity(os.lstat(held))
                replacement_identity = core.node_identity(os.lstat(live))
                if replacement_identity == held_identity:
                    raise AssertionError("same-byte live replacement reused the claimed inode")
                live_mode = "external"
            else:
                live_mode = "absent"
        else:
            epoch = len(attempts)
            if epoch > 9999:
                raise AssertionError("nonlatest unfinished fixture exhausted lock epochs")
            later = attempt_root / f"{epoch:04d}-abort"
            later.mkdir()
            later_identity = core.node_identity(os.lstat(later))
            core.write_json_create_only(
                attempt_root / f"{later.name}.claim.json",
                core._lock_attempt_root_claim_contract(
                    install_plan,
                    epoch,
                    "abort",
                    later_identity,
                ),
                "adversarial later abort attempt parent claim",
            )
            core.write_json_create_only(
                later / "owner.json",
                core._lock_attempt_owner_contract(
                    install_plan,
                    epoch,
                    "abort",
                    later_identity,
                ),
                "adversarial later abort attempt owner",
            )

        attempt_before = optional_bound_tree_fingerprint(attempt_root)
        protected_paths = [live]
        if held is not None:
            protected_paths.append(held)
        protected_before = {
            str(path): file_observation(path) for path in protected_paths
        }
        if runtime_fingerprint(fixture) != installed_runtime:
            raise AssertionError(
                f"abort unfinished-lock {variant} unexpectedly changed runtime"
            )
        blocked, blocked_json = invoke_abort(fixture, apply=True)
        protected_after = {
            str(path): file_observation(path) for path in protected_paths
        }
        if (
            blocked.returncode != 2
            or blocked_json.get("status") != "blocked"
            or optional_bound_tree_fingerprint(attempt_root) != attempt_before
            or not core.exact_json_equal(protected_after, protected_before)
            or runtime_fingerprint(fixture) != installed_runtime
        ):
            raise AssertionError(
                f"abort unfinished-lock {variant} was normalized or replayed: "
                f"{blocked_json}"
            )
        results.append(
            _assert_abort_outcome_unknown_latch(
                fixture,
                f"abort_unfinished_lock_{variant}_manual_latch",
                allow_owned_live_lock=live_mode == "owned",
                allow_preserved_external_live_lock=live_mode == "external",
            )
        )
    return results


def _raise_external_attack_assertion(
    label: str,
    caught_error: Optional[core.BootstrapError],
    **state: bool,
) -> None:
    """Fail closed without discarding the manager refusal that preempted a hook."""

    state_text = "; ".join(f"{key}={value}" for key, value in state.items())
    diagnostic = caught_error.diagnostic if caught_error is not None else None
    raise AssertionError(
        f"{label}: caught_bootstrap_error={caught_error!s}; "
        f"{state_text}; diagnostic={diagnostic!r}"
    ) from caught_error


def run_abort_external_replacement_latches(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    fixture = make_fixture(base, f"r{round_index}-abort-bridge-target-replacement")
    _before, _clone, _runtime = prepare_installed_staged_abort(fixture)
    fired = False
    held = fixture.test_root / "attacker-held-runtime-bridge"
    parsed = parse_abort_apply_args(fixture)
    roots = core.resolve_roots(parsed)
    stage, _ = core.stable_json(
        fixture.manifest.parent / "runtime-bootstrap-abort-stage.json",
        "abort moved-Bridge replacement stage",
    )
    abort_plan = stage.get("abort_plan")
    if not isinstance(abort_plan, Mapping):
        raise AssertionError("abort moved-Bridge replacement stage has no plan")
    explicit = {
        "pre_boundary": core.normalized(parsed.pre_boundary),
        "clone_manifest": core.normalized(parsed.clone_manifest),
        "source_save": core.normalized(parsed.source_save),
        "loader_save": core.normalized(parsed.loader_save),
    }
    explicit["source_preview"] = explicit["source_save"].with_suffix(".png")
    explicit["loader_preview"] = explicit["loader_save"].with_suffix(".png")
    target = core._abort_role_paths(roots, abort_plan, explicit)[
        "abort_runtime_bridge"
    ]

    def replace_moved_bridge(point: str) -> None:
        nonlocal fired
        if point == "after_abort_runtime_bridge_archived_physical" and not fired:
            fired = True
            target.rename(held)
            shutil.copytree(held, target, copy_function=shutil.copy2)

    caught_error: Optional[core.BootstrapError] = None
    try:
        core.execute_abort(
            parse_abort_apply_args(fixture),
            hook=replace_moved_bridge,
        )
    except core.BootstrapError as error:
        caught_error = error
    else:
        raise AssertionError("abort moved-Bridge external replacement was accepted")
    held_exists = held.is_dir()
    target_exists = target.is_dir()
    if not fired or not held_exists or not target_exists:
        _raise_external_attack_assertion(
            "abort moved-Bridge attack did not preserve both trees",
            caught_error,
            hook_fired=fired,
            held_exists=held_exists,
            target_exists=target_exists,
        )
    results.append(
        _assert_abort_outcome_unknown_latch(
            fixture,
            "abort_moved_bridge_external_replacement_latched",
            allow_owned_live_lock=True,
        )
    )

    fixture = make_fixture(base, f"r{round_index}-abort-mtime-parent-replacement")
    _before, _clone, _runtime = prepare_installed_staged_abort(fixture)
    fired = False
    parent = fixture.oni / "mods" / "Dev"
    held = fixture.test_root / "attacker-held-probe-parent"
    foreign_identity: Optional[list[int]] = None
    foreign_mtime: Optional[int] = None
    held_identity: Optional[list[int]] = None
    held_mtime: Optional[int] = None

    def replace_mtime_parent(point: str) -> None:
        nonlocal fired, foreign_identity, foreign_mtime, held_identity, held_mtime
        if point == "after_abort_probe_parent_mtime_restored_handle_open" and not fired:
            fired = True
            parent.rename(held)
            held_metadata = os.lstat(held)
            held_identity = core.node_identity(held_metadata)
            held_mtime = int(held_metadata.st_mtime_ns)
            shutil.copytree(held, parent, copy_function=shutil.copy2)
            metadata = os.lstat(parent)
            foreign_mtime = int(metadata.st_mtime_ns) + 20_000_000
            os.utime(parent, ns=(int(metadata.st_atime_ns), foreign_mtime))
            foreign_identity = core.node_identity(os.lstat(parent))

    caught_error = None
    try:
        core.execute_abort(
            parse_abort_apply_args(fixture),
            hook=replace_mtime_parent,
        )
    except core.BootstrapError as error:
        caught_error = error
    else:
        raise AssertionError("abort mtime parent replacement was accepted")
    current = os.lstat(parent)
    if (
        not fired
        or foreign_identity is None
        or foreign_mtime is None
        or held_identity is None
        or held_mtime is None
        or core.node_identity(current) != foreign_identity
        or int(current.st_mtime_ns) != foreign_mtime
        or not held.is_dir()
        or core.node_identity(os.lstat(held)) != held_identity
        or int(os.lstat(held).st_mtime_ns) != held_mtime
    ):
        _raise_external_attack_assertion(
            "abort mtime open-handle guard modified the external replacement",
            caught_error,
            hook_fired=fired,
            foreign_identity_captured=foreign_identity is not None,
            foreign_mtime_captured=foreign_mtime is not None,
            held_identity_captured=held_identity is not None,
            held_mtime_captured=held_mtime is not None,
            held_exists=held.is_dir(),
            parent_exists=parent.is_dir(),
        )
    results.append(
        _assert_abort_outcome_unknown_latch(
            fixture,
            "abort_mtime_external_parent_preserved_and_latched",
            allow_owned_live_lock=True,
        )
    )

    fixture = make_fixture(base, f"r{round_index}-abort-mtime-ancestor-swap")
    _before, _clone, _runtime = prepare_installed_staged_abort(fixture)
    fired = False
    container = fixture.oni / "mods"
    target = container / "Dev"
    held_container = fixture.test_root / "attacker-held-mods"
    held_target_identity: Optional[list[int]] = None
    held_target_mtime: Optional[int] = None
    foreign_target_identity: Optional[list[int]] = None
    foreign_target_mtime: Optional[int] = None

    def replace_mtime_parent_container(point: str) -> None:
        nonlocal fired, held_target_identity, held_target_mtime
        nonlocal foreign_target_identity, foreign_target_mtime
        # Windows can deny renaming an ancestor while a descendant directory
        # handle is open even with FILE_SHARE_DELETE.  Interpose at the final
        # pre-handle checkpoint instead: the manager must reject the changed
        # ancestor/target identity before SetFileTime touches either tree.
        if point == "before_abort_probe_parent_mtime_restored_physical" and not fired:
            fired = True
            container.rename(held_container)
            held_metadata = os.lstat(held_container / "Dev")
            held_target_identity = core.node_identity(held_metadata)
            held_target_mtime = int(held_metadata.st_mtime_ns)
            shutil.copytree(held_container, container, copy_function=shutil.copy2)
            foreign_metadata = os.lstat(target)
            foreign_target_mtime = int(foreign_metadata.st_mtime_ns) + 30_000_000
            os.utime(
                target,
                ns=(int(foreign_metadata.st_atime_ns), foreign_target_mtime),
            )
            foreign_target_identity = core.node_identity(os.lstat(target))

    caught_error = None
    try:
        core.execute_abort(
            parse_abort_apply_args(fixture),
            hook=replace_mtime_parent_container,
        )
    except core.BootstrapError as error:
        caught_error = error
    else:
        raise AssertionError("abort mtime ancestor replacement was accepted")
    held_target = held_container / "Dev"
    if (
        not fired
        or held_target_identity is None
        or held_target_mtime is None
        or foreign_target_identity is None
        or foreign_target_mtime is None
        or not held_target.is_dir()
        or not target.is_dir()
        or core.node_identity(os.lstat(held_target)) != held_target_identity
        or int(os.lstat(held_target).st_mtime_ns) != held_target_mtime
        or core.node_identity(os.lstat(target)) != foreign_target_identity
        or int(os.lstat(target).st_mtime_ns) != foreign_target_mtime
    ):
        _raise_external_attack_assertion(
            "abort mtime ancestor swap modified either directory tree",
            caught_error,
            hook_fired=fired,
            held_target_identity_captured=held_target_identity is not None,
            held_target_mtime_captured=held_target_mtime is not None,
            foreign_target_identity_captured=foreign_target_identity is not None,
            foreign_target_mtime_captured=foreign_target_mtime is not None,
            held_target_exists=held_target.is_dir(),
            target_exists=target.is_dir(),
        )
    results.append(
        _assert_abort_outcome_unknown_latch(
            fixture,
            "abort_mtime_external_ancestor_preserved_and_latched",
            allow_owned_live_lock=True,
        )
    )
    return results


def run_abort_missing_root_latch_cases(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # A final or partially published manual-recovery latch is authoritative
    # before any new physical attempt.  The handle-based mtime implementation
    # never renames oni_root itself, so there is no legitimate "missing root
    # claim" for generic abort to adopt.
    for suffix in ("", ".new"):
        label = "final" if not suffix else "partial"
        fixture = make_fixture(base, f"r{round_index}-abort-root-latch-{label}")
        _before, _clone, installed_runtime = prepare_installed_staged_abort(fixture)
        latch = transaction_dir(fixture) / "abort" / ("outcome-unknown.json" + suffix)
        latch.parent.mkdir(parents=True, exist_ok=True)
        latch.write_bytes(b'{"status":"external-manual-review"}\n')
        before_retry = optional_bound_tree_fingerprint(fixture.test_root)
        refused, refused_json = invoke_abort(fixture, apply=True)
        if (
            refused.returncode != 2
            or refused_json.get("status") != "blocked"
            or "outcome_unknown" not in str(refused_json).casefold()
            or optional_bound_tree_fingerprint(fixture.test_root) != before_retry
            or runtime_fingerprint(fixture) != installed_runtime
            or latch.read_bytes() != b'{"status":"external-manual-review"}\n'
        ):
            raise AssertionError(
                f"preexisting {label} latch permitted physical replay: {refused_json}"
            )
        assert_no_abort_result(fixture, f"preexisting {label} latch")
        assert_no_live_environment_lock(fixture, f"preexisting {label} latch")
        results.append(
            {
                "name": f"abort_preexisting_{label}_latch_blocks_recovery",
                "status": "passed",
                "classification": "manual_recovery_latch",
            }
        )

    fixture = make_fixture(base, f"r{round_index}-abort-root-missing")
    _before, _clone, _runtime = prepare_installed_staged_abort(fixture)
    held = fixture.test_root / "attacker-held-oni-root"
    fixture.oni.rename(held)
    held_before = optional_bound_tree_fingerprint(held)
    refused, refused_json = invoke_abort(fixture, apply=True)
    if (
        refused.returncode != 2
        or refused_json.get("status") != "blocked"
        or os.path.lexists(fixture.oni)
        or optional_bound_tree_fingerprint(held) != held_before
    ):
        raise AssertionError(
            f"externally missing ONI root was adopted or changed: {refused_json}"
        )
    assert_no_abort_result(fixture, "externally missing ONI root")
    assert_no_live_environment_lock(fixture, "externally missing ONI root")
    results.append(
        {
            "name": "abort_external_missing_oni_root_refused_zero_write",
            "status": "passed",
            "classification": "external_identity_attack",
        }
    )
    return results


def _assert_initialization_outcome_unknown_latch(
    fixture: Fixture,
    *,
    interrupted_rank: int,
) -> dict[str, Any]:
    roots = core.resolve_roots(parse_apply_args(fixture))
    latch_path = (
        fixture.manifest.parent
        / "runtime-bootstrap-initialization-outcome-unknown.json"
    )
    latch, _ = core.stable_json(
        latch_path,
        "runtime bootstrap initialization outcome_unknown latch",
    )
    latch_state = core.stable_file(
        latch_path,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    exact_keys = {
        "schema",
        "status",
        "classification",
        "run_id",
        "transaction_id",
        "plan_hash",
        "install_stage_sha256",
        "install_stage_hash",
        "support_nodes",
        "failure_type",
        "failure_hash",
        "manual_recovery_required",
        "runtime_mutation_started",
        "game_launched",
        "latch_hash",
    }
    unsigned = dict(latch)
    unsigned.pop("latch_hash", None)
    stage, stage_state = core.stable_json(
        fixture.manifest.parent / "runtime-bootstrap-install-stage.json",
        "runtime bootstrap external stage",
    )
    plan = stage.get("install_plan")
    support = latch.get("support_nodes")
    if (
        set(latch) != exact_keys
        or latch.get("schema")
        != "oni.live-runtime-bootstrap-initialization-outcome-unknown.v1"
        or latch.get("status") != "outcome_unknown"
        or latch.get("classification") != "infra_fail"
        or latch.get("run_id") != fixture.run_id
        or not isinstance(plan, Mapping)
        or latch.get("transaction_id") != plan.get("transaction_id")
        or latch.get("plan_hash") != plan.get("plan_hash")
        or latch.get("install_stage_sha256") != stage_state.get("sha256")
        or latch.get("install_stage_hash") != stage.get("stage_hash")
        or not isinstance(support, list)
        or len(support) != len(INITIALIZATION_CLAIM_POINTS)
        or not isinstance(latch.get("failure_type"), str)
        or core.HASH_RE.fullmatch(str(latch.get("failure_hash") or "")) is None
        or latch.get("manual_recovery_required") is not True
        or latch.get("runtime_mutation_started") is not False
        or latch.get("game_launched") is not False
        or latch.get("latch_hash") != core.canonical_hash(unsigned)
    ):
        raise AssertionError(f"initialization outcome_unknown latch is invalid: {latch}")
    specs = core._initialization_support_specs(roots)
    observation_key_sets = {
        "absent": {"status"},
        "unreadable_or_changed": {"status"},
        "present": {"status", "kind", "identity", "link_count", "length"},
    }
    for rank, item in enumerate(support):
        expected_rank, expected_node, expected_kind = specs[rank]
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {
                "rank",
                "relative",
                "expected_kind",
                "node",
                "claim",
                "claim_partial",
            }
            or item.get("rank") != expected_rank
            or item.get("relative")
            != expected_node.relative_to(roots.transaction_dir.parent).as_posix()
            or item.get("expected_kind") != expected_kind
        ):
            raise AssertionError(f"initialization latch support row {rank} is invalid")
        for field in ("node", "claim", "claim_partial"):
            observation = item.get(field)
            status = observation.get("status") if isinstance(observation, Mapping) else None
            if (
                status not in observation_key_sets
                or set(observation) != observation_key_sets[status]
            ):
                raise AssertionError(
                    f"initialization latch {field} observation {rank} is not exact"
                )
        expected_node_status = "present" if rank <= interrupted_rank else "absent"
        expected_claim_status = "present" if rank < interrupted_rank else "absent"
        if (
            item["node"]["status"] != expected_node_status
            or item["claim"]["status"] != expected_claim_status
            or item["claim_partial"]["status"] != "absent"
        ):
            raise AssertionError(
                f"initialization latch support observation {rank} does not match crash rank"
            )
    return {"latch": latch, "state": latch_state}


def run_initialization_outcome_unknown_cases(
    base: pathlib.Path,
    round_index: int,
    *,
    ranks: Optional[Sequence[int]] = None,
    include_preplants: bool = True,
    include_publication: bool = True,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    selected_ranks = list(range(len(INITIALIZATION_PRECLAIM_FAULT_POINTS)))
    if ranks is not None:
        selected_ranks = list(ranks)
    if (
        any(type(rank) is not int for rank in selected_ranks)
        or len(set(selected_ranks)) != len(selected_ranks)
        or any(rank < 0 or rank >= len(INITIALIZATION_PRECLAIM_FAULT_POINTS) for rank in selected_ranks)
    ):
        raise AssertionError("initialization targeted rank selection is invalid")
    for suffix in (("", ".new") if include_preplants else ()):
        label = "final" if not suffix else "partial"
        fixture = make_fixture(base, f"r{round_index}-init-latch-preplant-{label}")
        staged, staged_json = stage_fixture(fixture)
        if staged.returncode != 0:
            raise AssertionError(f"initialization latch preplant could not stage: {staged_json}")
        latch_path = (
            fixture.manifest.parent
            / ("runtime-bootstrap-initialization-outcome-unknown.json" + suffix)
        )
        payload = b'{"status":"external-preplant"}\n'
        latch_path.write_bytes(payload)
        identity = core.node_identity(os.lstat(latch_path))
        runtime_before = runtime_fingerprint(fixture)
        blocked, blocked_json = invoke(fixture, apply=True)
        if (
            blocked.returncode != 2
            or blocked_json.get("status") != "blocked"
            or latch_path.read_bytes() != payload
            or core.node_identity(os.lstat(latch_path)) != identity
            or runtime_fingerprint(fixture) != runtime_before
            or os.path.lexists(transaction_dir(fixture))
        ):
            raise AssertionError(
                f"initialization {label} latch preplant was adopted: {blocked_json}"
            )
        results.append(
            {
                "name": f"initialization_{label}_latch_preplant_refused",
                "status": "passed",
                "classification": "create_only_latch_ownership",
            }
        )

    for rank in selected_ranks:
        point = INITIALIZATION_PRECLAIM_FAULT_POINTS[rank]
        token = core.sha256_bytes(point.encode("utf-8")).removeprefix("sha256:")[:10]
        fixture = make_fixture(base, f"r{round_index}-init-gap-{token}")
        runtime_before = runtime_fingerprint(fixture)
        interrupted, interrupted_json = invoke(fixture, apply=True, fail_at=point)
        latch_path = (
            fixture.manifest.parent
            / "runtime-bootstrap-initialization-outcome-unknown.json"
        )
        if (
            interrupted.returncode != 99
            or interrupted_json.get("status") != "interrupted"
            or os.path.lexists(latch_path)
            or os.path.lexists(latch_path.with_name(latch_path.name + ".new"))
            or runtime_fingerprint(fixture) != runtime_before
        ):
            raise AssertionError(
                f"initialization preclaim crash {point} did not retain its exact gap: "
                f"{interrupted_json}"
            )
        blocked, blocked_json = invoke(fixture, apply=True)
        if (
            blocked.returncode != 2
            or blocked_json.get("status") != "blocked"
            or runtime_fingerprint(fixture) != runtime_before
        ):
            raise AssertionError(
                f"initialization preclaim crash {point} was replayed: {blocked_json}"
            )
        latch = _assert_initialization_outcome_unknown_latch(
            fixture,
            interrupted_rank=rank,
        )
        before_retry = optional_bound_tree_fingerprint(fixture.test_root)
        retried, retried_json = invoke(fixture, apply=True)
        if (
            retried.returncode != 2
            or retried_json.get("status") != "blocked"
            or "outcome_unknown" not in str(retried_json).casefold()
            or optional_bound_tree_fingerprint(fixture.test_root) != before_retry
            or runtime_fingerprint(fixture) != runtime_before
            or not core.exact_json_equal(
                core.stable_file(
                    latch_path,
                    max_bytes=core.MAX_JSON_BYTES,
                    collect=False,
                ),
                latch["state"],
            )
        ):
            raise AssertionError(
                f"initialization latch {point} allowed a later retry: {retried_json}"
            )
        assert_no_ready_result(fixture, f"initialization latch {point}")
        assert_no_live_environment_lock(fixture, f"initialization latch {point}")
        results.append(
            {
                "name": f"initialization_unclaimed_support_latched:{point}",
                "status": "passed",
                "classification": "outcome_unknown_manual_recovery",
            }
        )

    # Crash after the create-only latch reached its final inode.  The next
    # attempt must treat the name itself as authoritative and never adopt or
    # rewrite it.
    if include_publication:
        fixture = make_fixture(base, f"r{round_index}-init-latch-return")
        runtime_before = runtime_fingerprint(fixture)
        first_point = INITIALIZATION_PRECLAIM_FAULT_POINTS[0]
        interrupted, _ = invoke(fixture, apply=True, fail_at=first_point)
        if interrupted.returncode != 99:
            raise AssertionError("initialization latch publication fixture missed its first crash")
        published, published_json = invoke(
            fixture,
            apply=True,
            fail_at="after_initialization_outcome_unknown_publish",
        )
        if published.returncode != 99 or published_json.get("status") != "interrupted":
            raise AssertionError(
                f"initialization latch post-publication crash did not fire: {published_json}"
            )
        latch = _assert_initialization_outcome_unknown_latch(fixture, interrupted_rank=0)
        latch_path = (
            fixture.manifest.parent
            / "runtime-bootstrap-initialization-outcome-unknown.json"
        )
        before_retry = optional_bound_tree_fingerprint(fixture.test_root)
        retried, retried_json = invoke(fixture, apply=True)
        if (
            retried.returncode != 2
            or retried_json.get("status") != "blocked"
            or optional_bound_tree_fingerprint(fixture.test_root) != before_retry
            or runtime_fingerprint(fixture) != runtime_before
            or not core.exact_json_equal(
                core.stable_file(
                    latch_path,
                    max_bytes=core.MAX_JSON_BYTES,
                    collect=False,
                ),
                latch["state"],
            )
        ):
            raise AssertionError(
                f"post-publication initialization latch was adopted: {retried_json}"
            )
        results.append(
            {
                "name": "initialization_latch_post_publication_crash_is_nonreplay",
                "status": "passed",
                "classification": "create_only_latch_recovery",
            }
        )
    return results


def _assert_install_outcome_unknown_latch(
    fixture: Fixture,
    *,
    expected_journal_status: str,
) -> dict[str, Any]:
    latch_path = transaction_dir(fixture) / "outcome-unknown.json"
    latch, _ = core.stable_json(
        latch_path,
        "runtime bootstrap install outcome_unknown latch",
    )
    latch_state = core.stable_file(
        latch_path,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    exact_keys = {
        "schema",
        "status",
        "classification",
        "run_id",
        "transaction_id",
        "plan_hash",
        "install_stage_sha256",
        "install_stage_hash",
        "journal",
        "role_locations",
        "failure_type",
        "failure_hash",
        "manual_recovery_required",
        "game_launched",
        "latch_hash",
    }
    unsigned = dict(latch)
    unsigned.pop("latch_hash", None)
    stage, stage_state = core.stable_json(
        fixture.manifest.parent / "runtime-bootstrap-install-stage.json",
        "runtime bootstrap external install stage",
    )
    plan = stage.get("install_plan")
    journal = latch.get("journal")
    role_locations = latch.get("role_locations")
    if (
        set(latch) != exact_keys
        or latch.get("schema")
        != "oni.live-runtime-bootstrap-install-outcome-unknown.v1"
        or latch.get("status") != "outcome_unknown"
        or latch.get("classification") != "infra_fail"
        or latch.get("run_id") != fixture.run_id
        or not isinstance(plan, Mapping)
        or latch.get("transaction_id") != plan.get("transaction_id")
        or latch.get("plan_hash") != plan.get("plan_hash")
        or latch.get("install_stage_sha256") != stage_state.get("sha256")
        or latch.get("install_stage_hash") != stage.get("stage_hash")
        or not isinstance(journal, Mapping)
        or set(journal) != {"status", "entry_count", "tip_hash"}
        or journal.get("status") != expected_journal_status
        or (
            expected_journal_status == "unverified"
            and (journal.get("entry_count") is not None or journal.get("tip_hash") is not None)
        )
        or not isinstance(role_locations, Mapping)
        or set(role_locations)
        != {
            "archive_root",
            "bridge_stage",
            "live_bridge",
            "live_probe",
            "old_bridge_archive",
            "old_probe_recovery",
            "probe_stage",
        }
        or any(
            not isinstance(value, Mapping)
            or set(value) != {"path_id_lower", "state"}
            or not isinstance(value.get("path_id_lower"), str)
            or not isinstance(value.get("state"), Mapping)
            for value in role_locations.values()
        )
        or not isinstance(latch.get("failure_type"), str)
        or core.HASH_RE.fullmatch(str(latch.get("failure_hash") or "")) is None
        or latch.get("manual_recovery_required") is not True
        or latch.get("game_launched") is not False
        or latch.get("latch_hash") != core.canonical_hash(unsigned)
    ):
        raise AssertionError(f"install outcome_unknown latch is invalid: {latch}")
    return {"latch": latch, "state": latch_state}


def run_malformed_committed_journal_classification(
    base: pathlib.Path,
    round_index: int,
) -> dict[str, Any]:
    """A malformed committed epoch cannot be downgraded to init recovery."""

    fixture = make_fixture(base, f"r{round_index}-malcommit")
    interrupted, interrupted_json = invoke(
        fixture,
        apply=True,
        fail_at="after_commit_verified_journal",
    )
    if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
        raise AssertionError(
            "malformed committed-journal fixture did not reach physical commit: "
            f"{interrupted_json}"
        )
    transaction = transaction_dir(fixture)
    commit_path = sorted((transaction / "journal").glob("*.json"))[-1]
    commit = json.loads(commit_path.read_text(encoding="utf-8"))
    if commit.get("event") != "commit_verified":
        raise AssertionError("malformed committed-journal fixture has no terminal commit event")
    if os.path.lexists(transaction / "result-candidate.json"):
        raise AssertionError("commit-journal crash unexpectedly published a result candidate")
    runtime_after_commit = runtime_fingerprint(fixture)
    live_lock = fixture.evidence / fixture.run_id / "environment" / ".finalize.lock"
    if not live_lock.is_file():
        raise AssertionError("committed hard-crash fixture lost its owned live lock custody")
    live_lock_state = core.stable_file(
        live_lock,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    live_lock_identity = core.node_identity(os.lstat(live_lock))
    attempt_root = transaction / "environment-lock-attempts"
    attempt_state = optional_bound_tree_fingerprint(attempt_root)
    published_plan_value = json.loads(
        (transaction / "plan.json").read_text(encoding="utf-8")
    )
    attempts = core._lock_attempt_directories(attempt_root, published_plan_value)
    if (
        len(attempts) != 1
        or attempts[0].name != "0000-install"
        or os.path.lexists(attempts[0] / "released.lock")
        or os.path.lexists(attempts[0] / "released.json")
    ):
        raise AssertionError("committed hard-crash fixture is not at the owned lock boundary")
    payload = commit_path.read_bytes()
    commit_path.write_bytes(payload[: max(1, len(payload) // 2)])
    corrupted_state = core.stable_file(
        commit_path,
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    observed_hooks: list[str] = []
    blocker = expect_direct_blocked(
        fixture,
        "malformed committed journal",
        hook=observed_hooks.append,
    )
    physical_replay_hooks = {
        point
        for point in observed_hooks
        if point
        in {
            "after_probe_stage_physical",
            "after_bridge_stage_physical",
            "after_archive_root_stage_physical",
            "after_archive_root_physical",
            "after_old_bridge_physical",
            "after_old_probe_physical",
            "after_new_probe_physical",
            "after_new_bridge_physical",
        }
    }
    initialization_latch = (
        fixture.manifest.parent
        / "runtime-bootstrap-initialization-outcome-unknown.json"
    )
    if (
        not blocker
        or runtime_fingerprint(fixture) != runtime_after_commit
        or physical_replay_hooks
        or not core.exact_json_equal(
            core.stable_file(
                commit_path,
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            corrupted_state,
        )
        or not core.exact_json_equal(
            core.stable_file(
                live_lock,
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            live_lock_state,
        )
        or core.node_identity(os.lstat(live_lock)) != live_lock_identity
        or optional_bound_tree_fingerprint(attempt_root) != attempt_state
        or os.path.lexists(initialization_latch)
        or os.path.lexists(initialization_latch.with_name(initialization_latch.name + ".new"))
    ):
        raise AssertionError(
            "malformed committed journal was replayed or misclassified as initialization: "
            f"blocker={blocker!r}, hooks={observed_hooks}"
        )
    latch = _assert_install_outcome_unknown_latch(
        fixture,
        expected_journal_status="unverified",
    )
    assert_no_ready_result(fixture, "malformed committed journal")

    before_retry = optional_bound_tree_fingerprint(fixture.test_root)
    retried, retried_json = invoke(fixture, apply=True)
    if (
        retried.returncode != 2
        or retried_json.get("status") != "blocked"
        or "outcome_unknown" not in str(retried_json).casefold()
        or optional_bound_tree_fingerprint(fixture.test_root) != before_retry
        or runtime_fingerprint(fixture) != runtime_after_commit
        or not core.exact_json_equal(
            core.stable_file(
                transaction / "outcome-unknown.json",
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            latch["state"],
        )
    ):
        raise AssertionError(
            "malformed committed-journal latch allowed physical replay: "
            f"{retried_json}"
        )
    return {
        "name": "malformed_committed_journal_is_install_outcome_unknown",
        "status": "passed",
        "classification": "outcome_unknown_manual_recovery",
    }


def _assert_completed_replay_noop(
    fixture: Fixture,
    label: str,
) -> dict[str, Any]:
    """Prove a completed replay is runtime-zero-write but evidence-auditable."""

    transaction = transaction_dir(fixture)
    roots, plan = published_plan(fixture)
    result, _ = core.stable_json(
        transaction / "result.json",
        f"{label} installed result",
    )
    immutable_paths = [
        transaction / "owner.json",
        transaction / "intent.json",
        transaction / "plan.json",
        transaction / "transaction.lock",
        transaction / "result-candidate.json",
        transaction / "result.json",
        *sorted((transaction / "journal").glob("*.json")),
    ]
    immutable_before = {
        path.relative_to(transaction).as_posix(): core.stable_file(
            path,
            max_bytes=core.MAX_JSON_BYTES,
            collect=False,
        )
        for path in immutable_paths
    }
    runtime_before = runtime_fingerprint(fixture)
    attempt_root = transaction / "environment-lock-attempts"
    attempt_root_identity = core.node_identity(os.lstat(attempt_root))
    attempts_before = core._lock_attempt_directories(attempt_root, plan)
    existing_attempts = {
        attempt.name: optional_bound_tree_fingerprint(attempt)
        for attempt in attempts_before
    }
    existing_parent_claims = {
        f"{attempt.name}.claim.json": core.stable_file(
            attempt_root / f"{attempt.name}.claim.json",
            max_bytes=core.MAX_JSON_BYTES,
            collect=False,
        )
        for attempt in attempts_before
    }
    names_before = {path.name for path in attempt_root.iterdir()}

    replay, replay_json = invoke(fixture, apply=True)
    expected_response = {
        "schema": "oni.live-runtime-bootstrap-replay.v1",
        "status": "already_installed",
        "classification": "static_probe",
        "run_id": fixture.run_id,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "result_hash": result["result_hash"],
        "replay": "refused_noop",
        "runtime_write_count": 0,
        "evidence_epoch_written": True,
        "game_launched": False,
    }
    if replay.returncode != 0 or not core.exact_json_equal(
        replay_json, expected_response
    ):
        raise AssertionError(
            f"{label} completed replay response is not exact: {replay_json}"
        )
    immutable_after = {
        path.relative_to(transaction).as_posix(): core.stable_file(
            path,
            max_bytes=core.MAX_JSON_BYTES,
            collect=False,
        )
        for path in immutable_paths
    }
    if (
        runtime_fingerprint(fixture) != runtime_before
        or not core.exact_json_equal(immutable_after, immutable_before)
        or core.node_identity(os.lstat(attempt_root)) != attempt_root_identity
    ):
        raise AssertionError(
            f"{label} completed replay changed runtime or immutable transaction evidence"
        )

    attempts_after = core._lock_attempt_directories(attempt_root, plan)
    if len(attempts_after) != len(attempts_before) + 1:
        raise AssertionError(f"{label} did not create exactly one replay lock epoch")
    for attempt in attempts_before:
        if optional_bound_tree_fingerprint(attempt) != existing_attempts[attempt.name]:
            raise AssertionError(f"{label} changed old lock attempt {attempt.name}")
        claim_name = f"{attempt.name}.claim.json"
        if not core.exact_json_equal(
            core.stable_file(
                attempt_root / claim_name,
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            existing_parent_claims[claim_name],
        ):
            raise AssertionError(f"{label} changed old lock parent claim {claim_name}")

    replay_attempt = attempts_after[-1]
    replay_claim_name = f"{replay_attempt.name}.claim.json"
    if (
        replay_attempt.name != f"{len(attempts_before):04d}-replay"
        or {path.name for path in attempt_root.iterdir()}
        != names_before | {replay_attempt.name, replay_claim_name}
        or {path.name for path in replay_attempt.iterdir()}
        != {
            "owner.json",
            "intent.json",
            "claim.json",
            "acquired.json",
            "released.lock",
            "released.json",
        }
    ):
        raise AssertionError(f"{label} replay epoch pathset is not exact")
    replay_state = core._load_lock_attempt(roots, plan, replay_attempt)
    claim = replay_state.get("claim")
    release, _ = core.stable_json(
        replay_attempt / "released.json",
        f"{label} replay release",
    )
    released_lock = core.stable_file(
        replay_attempt / "released.lock",
        max_bytes=core.MAX_JSON_BYTES,
        collect=True,
    )
    if (
        replay_state.get("purpose") != "replay"
        or replay_state.get("released") is not True
        or replay_state.get("release_complete") is not True
        or not isinstance(claim, Mapping)
        or not core.exact_json_equal(release, core._lock_release_contract(claim))
        or released_lock.get("identity") != claim.get("lock_identity")
        or released_lock.get("sha256") != claim.get("payload_sha256")
        or released_lock.get("bytes") != replay_state.get("payload")
    ):
        raise AssertionError(f"{label} replay lock claim/release custody is invalid")
    assert_no_live_environment_lock(fixture, label)
    return replay_json


def run_fault_case(base: pathlib.Path, point: str, round_index: int) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-fault-{point}")
    interrupted, interrupted_json = invoke(fixture, apply=True, fail_at=point)
    if interrupted.returncode != 99 or interrupted_json.get("status") != "interrupted":
        raise AssertionError(f"fault {point} did not interrupt at its checkpoint: {interrupted_json}")
    recovered, recovered_json = invoke(fixture, apply=True)
    if recovered.returncode != 0:
        raise AssertionError(f"fault {point} did not recover: {recovered_json}")
    assert_installed(fixture, recovered_json)
    _assert_completed_replay_noop(fixture, f"fault recovery {point}")
    return {
        "name": f"fault_recovery:{point}",
        "status": "passed",
        "classification": "infra_fail_injected_then_converged",
    }


def run_what_if(base: pathlib.Path, round_index: int) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-what-if")
    probe_before = tree_fingerprint(fixture.oni / pathlib.Path(*core.PROBE_RELATIVE.parts))
    bridge_before = tree_fingerprint(fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts))
    result, value = forecast_install_stage(fixture)
    if (
        result.returncode != 0
        or value.get("status") != "would_install"
        or value.get("write_count") != 0
        or transaction_dir(fixture).exists()
        or (fixture.manifest.parent / "runtime-bootstrap-install-stage.json").exists()
        or probe_before != tree_fingerprint(fixture.oni / pathlib.Path(*core.PROBE_RELATIVE.parts))
        or bridge_before != tree_fingerprint(fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts))
    ):
        raise AssertionError(f"WhatIf was not zero-write: {value}")
    return {"name": "what_if_zero_write", "status": "passed"}


def run_windows_long_archive_what_if(
    base: pathlib.Path,
    round_index: int,
) -> dict[str, Any]:
    """Exercise a real over-MAX_PATH archive through logical-path WhatIf I/O."""

    if os.name != "nt":
        return {
            "name": "windows_long_archive_what_if_zero_write",
            "status": "passed",
            "classification": "platform_not_applicable",
        }

    fixture = make_fixture(base, f"r{round_index}-windows-long-archive-what-if")
    archive_root = fixture.oni / pathlib.Path(*core.ARCHIVE_ROOT_RELATIVE.parts)
    logical_file = core.normalized(
        archive_root
        / ("archive-" + "a" * 112)
        / ("metadata-" + "b" * 112)
        / "environment-metadata.json"
    )
    logical_parent = logical_file.parent
    payload = b'{"archive":"long-path-runtime-bootstrap"}\n'

    def extended_path(path: pathlib.Path | str) -> str:
        text = os.path.abspath(os.path.normpath(os.fspath(path)))
        if text.startswith(("\\\\?\\", "\\\\.\\")):
            return text
        if text.startswith("\\\\"):
            return "\\\\?\\UNC\\" + text[2:]
        return "\\\\?\\" + text

    def utf16_units(path: pathlib.Path | str) -> int:
        return len(os.fspath(path).encode("utf-16-le")) // 2

    def contains_extended_prefix(value: Any) -> bool:
        if isinstance(value, str):
            return "\\\\?\\" in value or "\\\\.\\" in value
        if isinstance(value, Mapping):
            return any(
                contains_extended_prefix(key) or contains_extended_prefix(item)
                for key, item in value.items()
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(contains_extended_prefix(item) for item in value)
        return False

    if utf16_units(logical_file) <= 260 or utf16_units(logical_parent) <= 260:
        raise AssertionError("long-archive fixture did not cross 260 UTF-16 units")
    if contains_extended_prefix(str(logical_file)):
        raise AssertionError("long-archive logical path already has a device prefix")

    real_lstat = os.lstat
    real_open = os.open
    real_scandir = os.scandir
    real_stat = os.stat
    observed = {
        phase: {operation: 0 for operation in ("lstat", "open", "scandir", "stat")}
        for phase in ("direct", "core")
    }
    phase = "direct"
    descriptor: Optional[int] = None
    patched = False

    def path_text(path: Any) -> Optional[str]:
        try:
            value = os.fspath(path)
        except TypeError:
            return None
        return value if isinstance(value, str) else None

    def is_unprefixed_long_path(path: Any) -> bool:
        text = path_text(path)
        return bool(
            text is not None
            and utf16_units(text) > 260
            and not text.startswith(("\\\\?\\", "\\\\.\\"))
        )

    def observe_extended(operation: str, path: Any) -> None:
        text = path_text(path)
        if text is not None and text.startswith("\\\\?\\"):
            observed[phase][operation] += 1

    def guarded_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        text = path_text(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy lstat refusal", text)
        observe_extended("lstat", path)
        return real_lstat(path, *args, **kwargs)

    def guarded_open(path: Any, *args: Any, **kwargs: Any) -> int:
        text = path_text(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy open refusal", text)
        observe_extended("open", path)
        return real_open(path, *args, **kwargs)

    def guarded_scandir(path: Any = ".") -> Any:
        text = path_text(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy scandir refusal", text)
        observe_extended("scandir", path)
        return real_scandir(path)

    def guarded_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        text = path_text(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy stat refusal", text)
        observe_extended("stat", path)
        return real_stat(path, *args, **kwargs)

    def expect_legacy_refusal(operation: str, callback: Callable[[], Any]) -> None:
        leaked: Any = None
        try:
            leaked = callback()
        except FileNotFoundError:
            return
        finally:
            if operation == "open" and type(leaked) is int:
                os.close(leaked)
            elif operation == "scandir" and leaked is not None:
                leaked.close()
        raise AssertionError(f"unprefixed long-path {operation} unexpectedly succeeded")

    try:
        os.makedirs(extended_path(logical_parent))
        descriptor = real_open(
            extended_path(logical_file),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.write(descriptor, payload)
        os.close(descriptor)
        descriptor = None

        core.os.lstat = guarded_lstat
        core.os.open = guarded_open
        core.os.scandir = guarded_scandir
        core.os.stat = guarded_stat
        patched = True

        expect_legacy_refusal("lstat", lambda: guarded_lstat(logical_file))
        expect_legacy_refusal(
            "open",
            lambda: guarded_open(
                logical_file,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            ),
        )
        expect_legacy_refusal("scandir", lambda: guarded_scandir(logical_parent))
        expect_legacy_refusal("stat", lambda: guarded_stat(logical_file))

        guarded_lstat(extended_path(logical_file))
        descriptor = guarded_open(
            extended_path(logical_file),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        os.close(descriptor)
        descriptor = None
        with guarded_scandir(extended_path(logical_parent)) as entries:
            if logical_file.name not in {entry.name for entry in entries}:
                raise AssertionError("extended scandir did not return the long-path file")
        guarded_stat(extended_path(logical_file))
        if any(observed["direct"][operation] < 1 for operation in observed["direct"]):
            raise AssertionError(
                f"extended direct syscall coverage is incomplete: {observed['direct']}"
            )

        phase = "core"
        archive_snapshot = core.snapshot_tree(archive_root)
        relative = logical_file.relative_to(core.normalized(archive_root)).as_posix()
        file_entries = [
            item
            for item in archive_snapshot.get("entries", [])
            if item.get("kind") == "file"
        ]
        if (
            len(file_entries) != 1
            or file_entries[0].get("relative") != relative
            or file_entries[0].get("sha256") != core.sha256_bytes(payload)
        ):
            raise AssertionError("long-archive snapshot lost logical relative metadata")

        manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
        manifest["backup"]["bridge_archive_root"]["snapshot"] = archive_snapshot
        write_json(fixture.manifest, manifest)
        fixture.manifest_hash = core.sha256_bytes(fixture.manifest.read_bytes())
        hash_index = fixture.args.index("--expected-environment-manifest-sha256") + 1
        fixture.args[hash_index] = fixture.manifest_hash
        create_environment_journal(
            fixture.manifest.parent / "restore-journal.jsonl",
            fixture.manifest_hash,
            pre_boundary_hash=core.sha256_bytes(fixture.pre_boundary.read_bytes()),
            clone_manifest_hash=core.sha256_bytes(fixture.clone_manifest.read_bytes()),
        )

        before = optional_bound_tree_fingerprint(fixture.test_root)
        _roots, intent, plan = deterministic_unpublished_install_contract(fixture)
        value = core.execute(parse_what_if_args(fixture))
        after = optional_bound_tree_fingerprint(fixture.test_root)
        transaction = transaction_dir(fixture)
        install_stage = fixture.manifest.parent / "runtime-bootstrap-install-stage.json"
        expected_path_id = core.sha256_bytes(
            str(logical_file).lower().encode("utf-8")
        )
        serialized_contract = json.loads(
            core.document_bytes({"intent": intent, "plan": plan, "result": value})
        )
        planned_entries = plan["prestate"]["archive_root"]["snapshot"]["entries"]
        if (
            value.get("status") != "would_install"
            or value.get("write_count") != 0
            or value.get("game_launched") is not False
            or os.path.lexists(transaction)
            or os.path.lexists(install_stage)
            or before != after
        ):
            raise AssertionError(f"long-archive WhatIf was not zero-write: {value}")
        if (
            not any(item.get("relative") == relative for item in planned_entries)
            or core.path_id(logical_file) != expected_path_id
            or contains_extended_prefix(serialized_contract)
            or contains_extended_prefix(relative)
        ):
            raise AssertionError(
                "extended prefix leaked into logical relative/path-id/plan serialization"
            )
        required_core_operations = ("lstat", "open", "scandir")
        if any(
            observed["core"][operation] < 1
            for operation in required_core_operations
        ):
            raise AssertionError(
                f"core long-path syscall coverage is incomplete: {observed['core']}"
            )
    finally:
        if patched:
            core.os.lstat = real_lstat
            core.os.open = real_open
            core.os.scandir = real_scandir
            core.os.stat = real_stat
        if descriptor is not None:
            os.close(descriptor)
        extended_archive_root = extended_path(archive_root)
        if os.path.lexists(extended_archive_root):
            shutil.rmtree(extended_archive_root)

    return {
        "name": "windows_long_archive_what_if_zero_write",
        "status": "passed",
        "classification": "windows_extended_path_testroot_only",
    }


def run_support_preclaim_attacks(
    base: pathlib.Path, round_index: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    # A syntactically exact stage bundle built with a different nonce is not
    # the caller-authorized origin.  It must be rejected before apply creates
    # any support node or reaches an ONI-root mutation.
    fixture = make_fixture(base, f"r{round_index}-external-init-intent")
    roots, install_intent, plan = deterministic_unpublished_install_contract(fixture)
    init_intent = core._initialization_intent(
        roots,
        install_intent,
        plan,
        "a" * 32,
    )
    if fixture.stage_nonce == "a" * 32:
        raise AssertionError("fixture stage nonce collided with adversarial nonce")
    init_path = fixture.manifest.parent / "runtime-bootstrap-install-stage.json"
    init_path.write_bytes(core.document_bytes(init_intent))
    external_identity = core.node_identity(os.lstat(init_path))
    runtime_before = runtime_fingerprint(fixture)
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 2 or value.get("status") != "blocked":
        raise AssertionError(f"external exact initialization intent was adopted: {value}")
    if (
        not os.path.lexists(init_path)
        or core.node_identity(os.lstat(init_path)) != external_identity
        or core.document_bytes(init_intent) != init_path.read_bytes()
        or os.path.lexists(roots.transaction_dir)
        or runtime_fingerprint(fixture) != runtime_before
    ):
        raise AssertionError(
            "external initialization intent changed or reached a transaction/runtime write"
        )
    results.append(
        {
            "name": "support_external_unbound_stage_authorization_refused",
            "status": "passed",
            "classification": "ownership_attack",
        }
    )

    # A pre-existing empty transaction directory has no inode-bound claim.  It
    # is indistinguishable from an external name replacement and must never be
    # silently adopted as this transaction's support root.
    fixture = make_fixture(base, f"r{round_index}-external-empty-transaction")
    transaction = transaction_dir(fixture)
    transaction.mkdir(parents=True)
    external_identity = core.node_identity(os.lstat(transaction))
    runtime_before = runtime_fingerprint(fixture)
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 2 or value.get("status") != "blocked":
        raise AssertionError(f"external empty transaction root was adopted: {value}")
    if (
        not os.path.lexists(transaction)
        or core.node_identity(os.lstat(transaction)) != external_identity
        or runtime_fingerprint(fixture) != runtime_before
    ):
        raise AssertionError("external empty transaction root was mutated or runtime changed")
    assert_no_ready_result(fixture, "external empty transaction root")
    results.append(
        {
            "name": "support_external_empty_transaction_refused",
            "status": "passed",
            "classification": "ownership_attack",
        }
    )

    # Attempt ownership must originate in a durable parent-level preclaim.  An
    # attacker can otherwise reproduce every deterministic byte after a crash.
    for label, owner_mode, stage_mode in (
        ("attempt_owner_partial", "partial", "none"),
        ("attempt_owner_complete", "complete", "none"),
        ("stage_unclaimed_empty", "complete", "unclaimed_empty"),
        ("stage_unclaimed_full", "complete", "unclaimed_full"),
        ("stage_partial_claim", "complete", "partial_claim"),
    ):
        fixture = make_fixture(base, f"r{round_index}-{label}")
        interrupted, _ = invoke(fixture, apply=True, fail_at="after_journal_initialized")
        if interrupted.returncode != 99:
            raise AssertionError(f"{label} fixture did not freeze its transaction plan")
        runtime_before = runtime_fingerprint(fixture)
        seed_external_lock_attempt(
            fixture, owner_mode=owner_mode, stage_mode=stage_mode
        )
        completed, value = invoke(fixture, apply=True)
        if completed.returncode != 2 or value.get("status") != "blocked":
            raise AssertionError(f"{label} was adopted as an owned lock epoch: {value}")
        if runtime_fingerprint(fixture) != runtime_before:
            raise AssertionError(f"{label} reached physical runtime mutation")
        assert_no_ready_result(fixture, label)
        assert_no_live_environment_lock(fixture, label)
        results.append(
            {
                "name": f"ownership_attack:{label}",
                "status": "passed",
                "classification": "ownership_attack",
            }
        )
    return results


def run_ready_publication_interposition(
    base: pathlib.Path, round_index: int
) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-ready-publication-interposition")
    live_lock = fixture.evidence / fixture.run_id / "environment" / ".finalize.lock"
    fired = False
    frozen_at_interposition: dict[str, Any] = {}

    def hook(point: str) -> None:
        nonlocal fired
        if point == "after_environment_lock_release" and not fired:
            fired = True
            transaction = transaction_dir(fixture)
            candidate_path = transaction / "result-candidate.json"
            roots, plan = published_plan(fixture)
            attempt_root = transaction / "environment-lock-attempts"
            attempts = core._lock_attempt_directories(attempt_root, plan)
            immutable_paths = [
                transaction / "owner.json",
                transaction / "intent.json",
                transaction / "plan.json",
                candidate_path,
                *sorted((transaction / "journal").glob("*.json")),
            ]
            transaction_lock = os.lstat(transaction / "transaction.lock")
            frozen_at_interposition.update(
                {
                    "runtime": runtime_fingerprint(fixture),
                    "candidate": core.stable_file(
                        candidate_path,
                        max_bytes=core.MAX_JSON_BYTES,
                        collect=False,
                    ),
                    "roots": roots,
                    "plan": plan,
                    "attempt_root_identity": core.node_identity(os.lstat(attempt_root)),
                    "attempt_names": {path.name for path in attempt_root.iterdir()},
                    "attempts": {
                        attempt.name: optional_bound_tree_fingerprint(attempt)
                        for attempt in attempts
                    },
                    "parent_claims": {
                        f"{attempt.name}.claim.json": core.stable_file(
                            attempt_root / f"{attempt.name}.claim.json",
                            max_bytes=core.MAX_JSON_BYTES,
                            collect=False,
                        )
                        for attempt in attempts
                    },
                    "immutable": {
                        path.relative_to(transaction).as_posix(): core.stable_file(
                            path,
                            max_bytes=core.MAX_JSON_BYTES,
                            collect=False,
                        )
                        for path in immutable_paths
                    },
                    "transaction_lock": {
                        "identity": core.node_identity(transaction_lock),
                        "length": int(transaction_lock.st_size),
                        "last_write_ns": int(transaction_lock.st_mtime_ns),
                    },
                }
            )
            # This is the exact cooperative-operation interposition window:
            # install released its epoch, while no ready publication epoch has
            # yet been acquired.  A safe manager must refuse before result.json.
            live_lock.write_bytes(b"foreign-config-or-abort-lock")
            frozen_at_interposition["foreign_lock"] = core.stable_file(
                live_lock,
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            )

    blocker = expect_direct_blocked(
        fixture, "ready publication interposition", hook=hook
    )
    if not fired:
        raise AssertionError("ready publication interposition hook was not reached")
    assert_no_ready_result(fixture, "ready publication interposition")
    transaction = transaction_dir(fixture)
    install_latch = transaction / "outcome-unknown.json"
    initialization_latch = (
        fixture.manifest.parent
        / "runtime-bootstrap-initialization-outcome-unknown.json"
    )
    roots = frozen_at_interposition.get("roots")
    plan = frozen_at_interposition.get("plan")
    if not isinstance(roots, core.Roots) or not isinstance(plan, Mapping):
        raise AssertionError("ready publication interposition lost its frozen contract")
    attempt_root = transaction / "environment-lock-attempts"
    immutable_paths = [
        transaction / "owner.json",
        transaction / "intent.json",
        transaction / "plan.json",
        transaction / "result-candidate.json",
        *sorted((transaction / "journal").glob("*.json")),
    ]
    immutable_after = {
        path.relative_to(transaction).as_posix(): core.stable_file(
            path,
            max_bytes=core.MAX_JSON_BYTES,
            collect=False,
        )
        for path in immutable_paths
    }
    transaction_lock = os.lstat(transaction / "transaction.lock")
    transaction_lock_after = {
        "identity": core.node_identity(transaction_lock),
        "length": int(transaction_lock.st_size),
        "last_write_ns": int(transaction_lock.st_mtime_ns),
    }
    attempts = core._lock_attempt_directories(attempt_root, plan)
    old_attempts = frozen_at_interposition.get("attempts")
    old_parent_claims = frozen_at_interposition.get("parent_claims")
    old_names = frozen_at_interposition.get("attempt_names")
    if (
        not isinstance(old_attempts, Mapping)
        or not isinstance(old_parent_claims, Mapping)
        or not isinstance(old_names, set)
        or len(attempts) != len(old_attempts) + 1
    ):
        raise AssertionError("ready publication contention has the wrong attempt delta")
    replay_attempt = attempts[-1]
    replay_claim_name = f"{replay_attempt.name}.claim.json"
    replay_state = core._load_lock_attempt(roots, plan, replay_attempt)
    old_attempts_unchanged = all(
        optional_bound_tree_fingerprint(attempt_root / name) == fingerprint
        for name, fingerprint in old_attempts.items()
    )
    old_claims_unchanged = all(
        core.exact_json_equal(
            core.stable_file(
                attempt_root / name,
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            state,
        )
        for name, state in old_parent_claims.items()
    )
    if (
        runtime_fingerprint(fixture) != frozen_at_interposition.get("runtime")
        or not core.exact_json_equal(
            core.stable_file(
                transaction / "result-candidate.json",
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            frozen_at_interposition.get("candidate"),
        )
        or not core.exact_json_equal(
            immutable_after,
            frozen_at_interposition.get("immutable"),
        )
        or not core.exact_json_equal(
            transaction_lock_after,
            frozen_at_interposition.get("transaction_lock"),
        )
        or core.node_identity(os.lstat(attempt_root))
        != frozen_at_interposition.get("attempt_root_identity")
        or not old_attempts_unchanged
        or not old_claims_unchanged
        or replay_attempt.name != f"{len(old_attempts):04d}-replay"
        or {path.name for path in attempt_root.iterdir()}
        != old_names | {replay_attempt.name, replay_claim_name}
        or {path.name for path in replay_attempt.iterdir()}
        != {"owner.json", "intent.json"}
        or replay_state.get("purpose") != "replay"
        or replay_state.get("claim") is not None
        or replay_state.get("released") is not False
        or not core.exact_json_equal(
            core.stable_file(
                live_lock,
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            frozen_at_interposition.get("foreign_lock"),
        )
        or os.path.lexists(install_latch)
        or os.path.lexists(install_latch.with_name(install_latch.name + ".new"))
        or os.path.lexists(initialization_latch)
        or os.path.lexists(
            initialization_latch.with_name(initialization_latch.name + ".new")
        )
    ):
        raise AssertionError(
            "ready publication lock contention mutated runtime/evidence or published a latch"
        )
    if "environment lock" not in blocker.casefold():
        raise AssertionError(f"unexpected interposition blocker: {blocker}")

    replay_attempt_identity = core.node_identity(os.lstat(replay_attempt))
    replay_owner_before = core.stable_file(
        replay_attempt / "owner.json",
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )
    replay_intent_before = core.stable_file(
        replay_attempt / "intent.json",
        max_bytes=core.MAX_JSON_BYTES,
        collect=False,
    )

    # Remove only the exact TestRoot file this scenario created; recovery must
    # then converge from the immutable install candidate without replaying the
    # physical install transaction.
    if live_lock.read_bytes() != b"foreign-config-or-abort-lock":
        raise AssertionError("foreign interposition lock changed before cleanup")
    live_lock.unlink()
    recovered, recovered_json = invoke(fixture, apply=True)
    if recovered.returncode != 0:
        raise AssertionError(
            f"candidate did not recover after interposition was cleared: {recovered_json}"
        )
    assert_installed(fixture, recovered_json)
    recovered_attempts = core._lock_attempt_directories(attempt_root, plan)
    if len(recovered_attempts) < 2:
        raise AssertionError("ready publication recovery lost its replay epochs")
    candidate_recovery_attempt = recovered_attempts[-2]
    ready_attempt = recovered_attempts[-1]
    candidate_recovery_state = core._load_lock_attempt(
        roots, plan, candidate_recovery_attempt
    )
    ready_state = core._load_lock_attempt(roots, plan, ready_attempt)
    ready_result, _ = core.stable_json(
        transaction / "result.json",
        "ready publication recovered result",
    )
    ready_release, _ = core.stable_json(
        ready_attempt / "released.json",
        "ready publication recovered release",
    )
    if (
        runtime_fingerprint(fixture) != frozen_at_interposition.get("runtime")
        or not core.exact_json_equal(
            core.stable_file(
                transaction / "result-candidate.json",
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            frozen_at_interposition.get("candidate"),
        )
        # Candidate recovery must resume the exact contention prefix, then a
        # distinct shared-lock epoch publishes the externally consumable ready
        # certificate.  No third fresh epoch is permitted.
        or len(recovered_attempts) != len(attempts) + 1
        or candidate_recovery_attempt.name != replay_attempt.name
        or core.node_identity(os.lstat(candidate_recovery_attempt))
        != replay_attempt_identity
        or not core.exact_json_equal(
            core.stable_file(
                replay_attempt / "owner.json",
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            replay_owner_before,
        )
        or not core.exact_json_equal(
            core.stable_file(
                replay_attempt / "intent.json",
                max_bytes=core.MAX_JSON_BYTES,
                collect=False,
            ),
            replay_intent_before,
        )
        or candidate_recovery_state.get("purpose") != "replay"
        or candidate_recovery_state.get("release_complete") is not True
        or {path.name for path in candidate_recovery_attempt.iterdir()}
        != {
            "owner.json",
            "intent.json",
            "claim.json",
            "acquired.json",
            "released.lock",
            "released.json",
        }
        or ready_attempt.name != f"{len(attempts):04d}-replay"
        or ready_state.get("purpose") != "replay"
        or ready_state.get("release_complete") is not True
        or {path.name for path in ready_attempt.iterdir()}
        != {
            "owner.json",
            "intent.json",
            "claim.json",
            "acquired.json",
            "released.lock",
            "released.json",
        }
        or ready_result.get("ready_lock_epoch")
        != int(ready_attempt.name.split("-", 1)[0])
        or ready_result.get("ready_lock_release_hash")
        != ready_release.get("release_hash")
    ):
        raise AssertionError(
            "ready publication recovery replayed physical install or rewrote its candidate"
        )
    assert_no_live_environment_lock(fixture, "ready publication recovery")
    return {
        "name": "ready_publication_requires_owned_replay_epoch",
        "status": "passed",
        "classification": "toctou_attack",
    }


def run_ready_release_suffix_recovery(
    base: pathlib.Path, round_index: int
) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-ready-release-suffix")
    physical_release_count = 0

    def hook(point: str) -> None:
        nonlocal physical_release_count
        if point == "after_environment_lock_release_physical":
            physical_release_count += 1
            if physical_release_count == 2:
                # First occurrence closes install; second closes the ready
                # publication epoch whose predicted release is in result.json.
                raise core.InjectedCrash("ready_release_physical_before_release_record")

    try:
        core.execute(parse_apply_args(fixture), hook=hook)
    except core.InjectedCrash:
        pass
    else:
        raise AssertionError("ready release suffix fault did not interrupt")
    if physical_release_count != 2:
        raise AssertionError("ready release suffix fault hit the wrong lock epoch")
    result_path = transaction_dir(fixture) / "result.json"
    if not result_path.is_file():
        raise AssertionError("ready release suffix fixture has no bound result")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    ready_epoch = result.get("ready_lock_epoch")
    if type(ready_epoch) is not int:
        raise AssertionError("ready result has no exact publication epoch")
    attempt = (
        transaction_dir(fixture)
        / "environment-lock-attempts"
        / f"{ready_epoch:04d}-replay"
    )
    if not (attempt / "released.lock").is_file() or (attempt / "released.json").exists():
        raise AssertionError("ready release suffix fixture is not at the intended crash boundary")
    recovered, recovered_json = invoke(fixture, apply=True)
    if recovered.returncode != 0:
        raise AssertionError(
            f"ready bound epoch release suffix did not recover: {recovered_json}"
        )
    assert_installed(fixture, recovered_json)
    bound_release = json.loads((attempt / "released.json").read_text(encoding="utf-8"))
    if bound_release.get("release_hash") != result.get("ready_lock_release_hash"):
        raise AssertionError("recovered ready release does not match result binding")
    return {
        "name": "ready_lock_physical_release_suffix_recovery",
        "status": "passed",
        "classification": "crash_recovery",
    }


def run_completed_replay_convergence(
    base: pathlib.Path, round_index: int
) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-completed-replay-convergence")
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 0:
        raise AssertionError(value)
    assert_installed(fixture, value)
    for replay_index in range(1, 4):
        _assert_completed_replay_noop(
            fixture,
            f"completed replay {replay_index}",
        )
    return {
        "name": "completed_replay_three_pass_convergence",
        "status": "passed",
        "classification": "nonreplay_runtime_with_audited_lock_epochs",
    }


def run_presence_cases(base: pathlib.Path, round_index: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name, options in (
        ("old_absent_archive_absent", {"old_probe": False, "old_bridge": False}),
        ("old_present_archive_present", {"archive_present": True}),
        ("empty_old_bridge", {}),
    ):
        fixture = make_fixture(base, f"r{round_index}-{name}", **options)
        if name == "empty_old_bridge":
            bridge = fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
            shutil.rmtree(bridge)
            bridge.mkdir()
            # Rebuild the manifest because environment prepare binds existence,
            # including an empty directory, not content count.
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            manifest["backup"]["old_bridge"]["snapshot"] = core.snapshot_tree(bridge)
            write_json(fixture.manifest, manifest)
            fixture.manifest_hash = core.sha256_bytes(fixture.manifest.read_bytes())
            create_environment_journal(
                fixture.manifest.parent / "restore-journal.jsonl",
                fixture.manifest_hash,
                pre_boundary_hash=core.sha256_bytes(fixture.pre_boundary.read_bytes()),
                clone_manifest_hash=core.sha256_bytes(fixture.clone_manifest.read_bytes()),
            )
            index = fixture.args.index("--expected-environment-manifest-sha256") + 1
            fixture.args[index] = fixture.manifest_hash
        completed, value = invoke(fixture, apply=True)
        if completed.returncode != 0:
            raise AssertionError(f"presence case {name} failed: {value}")
        assert_installed(fixture, value)
        bootstrap = json.loads(
            (
                fixture.oni
                / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
                / "v27-runtime-bootstrap.json"
            ).read_text(encoding="utf-8")
        )
        if name == "old_absent_archive_absent" and bootstrap["archived_previous_bridge"] is not None:
            raise AssertionError("absent old Bridge unexpectedly named an archive")
        if name in {"old_present_archive_present", "empty_old_bridge"}:
            archive = pathlib.Path(bootstrap["archived_previous_bridge"])
            if not archive.exists() or archive.parent != fixture.oni / pathlib.Path(
                *core.ARCHIVE_ROOT_RELATIVE.parts
            ):
                raise AssertionError("old Bridge was not archived as one direct child")
        results.append({"name": name, "status": "passed"})
    return results


def reseal_journal(transaction: pathlib.Path) -> None:
    previous = None
    for path in sorted((transaction / "journal").glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["previous_entry_hash"] = previous
        unsigned = dict(entry)
        unsigned.pop("entry_hash", None)
        entry["entry_hash"] = core.canonical_hash(unsigned)
        previous = entry["entry_hash"]
        write_json(path, entry)


def reseal_plan_attack(fixture: Fixture, mutation: Optional[Callable[[dict[str, Any]], None]] = None) -> None:
    transaction = transaction_dir(fixture)
    plan_path = transaction / "plan.json"
    intent_path = transaction / "intent.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if mutation is None:
        plan["desired"]["action_policy"]["actions_enabled"] = True
    else:
        mutation(plan)
    unhashed = dict(plan)
    unhashed.pop("plan_hash", None)
    plan["plan_hash"] = core.canonical_hash(unhashed)
    write_json(plan_path, plan)
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["plan_hash"] = plan["plan_hash"]
    intent_unhashed = dict(intent)
    intent_unhashed.pop("intent_hash", None)
    intent["intent_hash"] = core.canonical_hash(intent_unhashed)
    write_json(intent_path, intent)
    for path in sorted((transaction / "journal").glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["plan_hash"] = plan["plan_hash"]
        if entry["event"] == "transaction_initialized":
            entry["data"]["plan_hash"] = plan["plan_hash"]
        write_json(path, entry)
    reseal_journal(transaction)


def expect_blocked(fixture: Fixture, label: str) -> dict[str, Any]:
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 2 or value.get("status") != "blocked":
        raise AssertionError(f"{label} was not blocked: {value}")
    if not transaction_dir(fixture).exists():
        raise AssertionError(f"{label} lost the transaction latch")
    return {"name": label, "status": "passed", "classification": "infra_fail"}


def run_typed_exact_cases(base: pathlib.Path, round_index: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    fixture = make_fixture(base, f"r{round_index}-typed-plan-length")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_journal_initialized")
    if interrupted.returncode != 99:
        raise AssertionError("typed plan fixture did not latch")

    def replace_length_with_bool(plan: dict[str, Any]) -> None:
        plan["inputs"]["release_artifacts"]["OniAiProbeMod.dll"]["length"] = False

    reseal_plan_attack(fixture, replace_length_with_bool)
    results.append(expect_blocked(fixture, "typed_plan_bool_int_reseal_refused"))

    fixture = make_fixture(base, f"r{round_index}-typed-result-bool")
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 0:
        raise AssertionError(value)
    result_path = transaction_dir(fixture) / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["shared_lock_released"] = 1
    unsigned = dict(result)
    unsigned.pop("result_hash", None)
    result["result_hash"] = core.canonical_hash(unsigned)
    write_json(result_path, result)
    results.append(expect_blocked(fixture, "typed_result_true_to_one_refused"))

    # Coherently rewrite both wrappers so the check cannot pass merely because
    # Python treats False == 0.  The exact integer epoch contract must still
    # reject the candidate before it can authorize replay.
    fixture = make_fixture(base, f"r{round_index}-typed-candidate-epoch")
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 0:
        raise AssertionError(value)
    candidate_path = transaction_dir(fixture) / "result-candidate.json"
    result_path = transaction_dir(fixture) / "result.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["install_lock_epoch"] = False
    candidate_unsigned = dict(candidate)
    candidate_unsigned.pop("candidate_hash", None)
    candidate["candidate_hash"] = core.canonical_hash(candidate_unsigned)
    write_json(candidate_path, candidate)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["install_lock_epoch"] = False
    result["candidate_hash"] = candidate["candidate_hash"]
    result["candidate_sha256"] = core.sha256_bytes(candidate_path.read_bytes())
    result_unsigned = dict(result)
    result_unsigned.pop("result_hash", None)
    result["result_hash"] = core.canonical_hash(result_unsigned)
    write_json(result_path, result)
    results.append(expect_blocked(fixture, "typed_candidate_epoch_false_refused"))

    # The ready wrappers carry a canonical projection of the one immutable
    # install commit journal record.  Rehashing that nested projection and both
    # wrappers must not replace the raw journal origin.
    fixture = make_fixture(base, f"r{round_index}-commit-projection-reseal")
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 0:
        raise AssertionError(value)
    candidate_path = transaction_dir(fixture) / "result-candidate.json"
    result_path = transaction_dir(fixture) / "result.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    forged_projection = copy.deepcopy(candidate["install_commit_entry"])
    forged_projection["data"]["probe_tree_content_hash"] = "sha256:" + "7" * 64
    forged_projection_hash = core.canonical_hash(forged_projection)
    candidate["install_commit_entry"] = forged_projection
    candidate["install_commit_entry_hash"] = forged_projection_hash
    candidate_unsigned = dict(candidate)
    candidate_unsigned.pop("candidate_hash", None)
    candidate["candidate_hash"] = core.canonical_hash(candidate_unsigned)
    write_json(candidate_path, candidate)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["install_commit_entry"] = forged_projection
    result["install_commit_entry_hash"] = forged_projection_hash
    result["candidate_hash"] = candidate["candidate_hash"]
    result["candidate_sha256"] = core.sha256_bytes(candidate_path.read_bytes())
    result_unsigned = dict(result)
    result_unsigned.pop("result_hash", None)
    result["result_hash"] = core.canonical_hash(result_unsigned)
    write_json(result_path, result)
    results.append(expect_blocked(fixture, "commit_projection_reseal_refused"))
    return results


def run_journal_attack_cases(base: pathlib.Path, round_index: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    fixture = make_fixture(base, f"r{round_index}-journal-index-bool")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_bridge_stage_journal")
    if interrupted.returncode != 99:
        raise AssertionError("journal bool-index fixture did not latch")
    journal_file = sorted((transaction_dir(fixture) / "journal").glob("*.json"))[-1]
    entry = json.loads(journal_file.read_text(encoding="utf-8"))
    entry["index"] = True
    write_json(journal_file, entry)
    reseal_journal(transaction_dir(fixture))
    results.append(expect_blocked(fixture, "journal_index_bool_reseal_refused"))

    fixture = make_fixture(base, f"r{round_index}-journal-gap")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_bridge_stage_journal")
    if interrupted.returncode != 99:
        raise AssertionError("journal gap fixture did not latch")
    files = sorted((transaction_dir(fixture) / "journal").glob("*.json"))
    files[len(files) // 2].unlink()
    results.append(expect_blocked(fixture, "journal_gap_refused"))

    fixture = make_fixture(base, f"r{round_index}-journal-truncate")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_bridge_stage_journal")
    if interrupted.returncode != 99:
        raise AssertionError("journal truncate fixture did not latch")
    journal_file = sorted((transaction_dir(fixture) / "journal").glob("*.json"))[-1]
    payload = journal_file.read_bytes()
    journal_file.write_bytes(payload[: max(1, len(payload) // 2)])
    results.append(expect_blocked(fixture, "journal_truncated_tail_refused"))

    fixture = make_fixture(base, f"r{round_index}-journal-cross-transaction")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_bridge_stage_journal")
    if interrupted.returncode != 99:
        raise AssertionError("journal cross-transaction fixture did not latch")
    for path in sorted((transaction_dir(fixture) / "journal").glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["transaction_id"] = "rtb-ffffffffffffffffffffffff"
        write_json(path, entry)
    reseal_journal(transaction_dir(fixture))
    results.append(expect_blocked(fixture, "journal_cross_transaction_reseal_refused"))

    fixture = make_fixture(base, f"r{round_index}-journal-duplicate-commit")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_result_candidate_publish")
    if interrupted.returncode != 99:
        raise AssertionError("journal duplicate-commit fixture did not reach committed candidate")
    journal_dir = transaction_dir(fixture) / "journal"
    files = sorted(journal_dir.glob("*.json"))
    commit = json.loads(files[-1].read_text(encoding="utf-8"))
    duplicate = copy.deepcopy(commit)
    duplicate["index"] = len(files) + 1
    duplicate["previous_entry_hash"] = commit["entry_hash"]
    unsigned = dict(duplicate)
    unsigned.pop("entry_hash", None)
    duplicate["entry_hash"] = core.canonical_hash(unsigned)
    write_json(
        journal_dir / f"{len(files) + 1:04d}-commit_verified.json", duplicate
    )
    results.append(expect_blocked(fixture, "journal_duplicate_commit_refused"))
    assert_no_ready_result(fixture, "journal duplicate commit")
    return results


def run_bound_rename_race_case(base: pathlib.Path, round_index: int) -> dict[str, Any]:
    fixture = make_fixture(base, f"r{round_index}-rename-name-swap-window")
    old_bridge = fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
    held_bridge = old_bridge.with_name("ai-bridge-original-held-by-attacker")
    real_rename = core.os.rename
    fired = False

    def hostile_rename(source: Any, target: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal fired
        source_path = pathlib.Path(source)
        if not fired and core.same_path(source_path, old_bridge):
            fired = True
            real_rename(source_path, held_bridge)
            shutil.copytree(held_bridge, old_bridge, copy_function=shutil.copy2)
        return real_rename(source, target, *args, **kwargs)

    core.os.rename = hostile_rename
    try:
        blocker = expect_direct_blocked(fixture, "bound rename name-swap")
    finally:
        core.os.rename = real_rename
    if not fired or "outcome_unknown" not in blocker:
        raise AssertionError(f"bound rename did not detect the interposed inode: {blocker}")
    if not held_bridge.exists():
        raise AssertionError("bound rename attack lost the external original tree")
    assert_no_ready_result(fixture, "bound rename name-swap")

    diagnostic_source = fixture.test_root / "rename-diagnostic-source"
    diagnostic_target = fixture.test_root / "rename-diagnostic-target"
    diagnostic_source.mkdir()
    (diagnostic_source / "sentinel.json").write_bytes(b'{"sentinel":true}\n')
    diagnostic_parent_identity = core.node_identity(os.lstat(fixture.test_root))
    diagnostic_snapshot, diagnostic_identity = core.capture_bound_tree(
        diagnostic_source
    )
    expected_root_identity = diagnostic_identity["root_identity"]
    synthetic_error = PermissionError(
        13,
        "synthetic sharing violation",
        str(diagnostic_source),
        32,
        str(diagnostic_target),
    )
    rename_call_count = 0

    def sharing_violation_rename(
        observed_source: Any,
        observed_target: Any,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        nonlocal rename_call_count
        rename_call_count += 1
        if (
            not core.same_path(pathlib.Path(observed_source), diagnostic_source)
            or not core.same_path(pathlib.Path(observed_target), diagnostic_target)
            or _args
            or _kwargs
        ):
            raise AssertionError("rename diagnostic intercepted the wrong endpoints")
        raise synthetic_error

    captured_error: Optional[core.BootstrapError] = None
    core.os.rename = sharing_violation_rename
    try:
        core.rename_bound_tree(
            diagnostic_source,
            diagnostic_target,
            expected_identity=diagnostic_identity,
            expected_snapshot=diagnostic_snapshot,
            label="diagnostic bound-tree rename",
        )
    except core.BootstrapError as exc:
        captured_error = exc
    else:
        raise AssertionError("synthetic sharing violation did not fail closed")
    finally:
        core.os.rename = real_rename
    if captured_error is None or rename_call_count != 1:
        raise AssertionError("rename diagnostic path retried or lost its refusal")
    refusal = core.bootstrap_error_document(captured_error)
    diagnostic = refusal.get("diagnostic")
    if (
        refusal.get("schema") != "oni.live-runtime-bootstrap-error.v1"
        or refusal.get("status") != "blocked"
        or refusal.get("classification") != "infra_fail"
        or refusal.get("error")
        != "diagnostic bound-tree rename atomic rename failed"
        or not isinstance(diagnostic, dict)
        or diagnostic.get("schema") != "oni.local-os-error-diagnostic.v1"
        or diagnostic.get("operation") != "rename"
        or diagnostic.get("operation_label") != "diagnostic bound-tree rename"
        or diagnostic.get("action_outcome") != "unknown"
        or diagnostic.get("native_error")
        != {
            "exception_type": "PermissionError",
            "errno": 13,
            "winerror": 32,
            "filename_present": True,
            "filename2_present": True,
            "filename_fields_redacted": True,
        }
        or diagnostic.get("source")
        != {
            "path_id_lower": core.path_id(diagnostic_source),
            "exists": True,
            "identity": expected_root_identity,
            "kind": "directory",
        }
        or diagnostic.get("target")
        != {
            "path_id_lower": core.path_id(diagnostic_target),
            "exists": False,
        }
    ):
        raise AssertionError(f"rename diagnostic contract drifted: {refusal}")
    serialized_refusal = json.dumps(refusal, ensure_ascii=False, sort_keys=True)
    if (
        diagnostic_source.name in serialized_refusal
        or diagnostic_target.name in serialized_refusal
        or "synthetic sharing violation" in serialized_refusal
    ):
        raise AssertionError("rename diagnostic disclosed a lexical path or native message")
    generic_refusal = core.unexpected_local_error_document(synthetic_error)
    if (
        generic_refusal
        != {
            "schema": "oni.live-runtime-bootstrap-error.v1",
            "status": "blocked",
            "classification": "infra_fail",
            "error": "unexpected local I/O or data-shape failure",
            "diagnostic": {
                "schema": "oni.local-os-error-diagnostic.v1",
                "operation": "local_io",
                "action_outcome": "unknown",
                "native_error": {
                    "exception_type": "PermissionError",
                    "errno": 13,
                    "winerror": 32,
                    "filename_present": True,
                    "filename2_present": True,
                    "filename_fields_redacted": True,
                },
            },
        }
        or diagnostic_source.name
        in json.dumps(generic_refusal, ensure_ascii=False, sort_keys=True)
        or diagnostic_target.name
        in json.dumps(generic_refusal, ensure_ascii=False, sort_keys=True)
        or "synthetic sharing violation"
        in json.dumps(generic_refusal, ensure_ascii=False, sort_keys=True)
    ):
        raise AssertionError("generic local I/O refusal lost or disclosed diagnostics")
    post_snapshot, post_identity = core.capture_bound_tree(diagnostic_source)
    if (
        not core.exact_json_equal(post_snapshot, diagnostic_snapshot)
        or not core.exact_json_equal(post_identity, diagnostic_identity)
        or core.node_identity(os.lstat(fixture.test_root))
        != diagnostic_parent_identity
        or os.path.lexists(diagnostic_target)
    ):
        raise AssertionError("failed diagnostic rename changed its TestRoot endpoints")

    file_source = fixture.test_root / "rename-file-diagnostic-source.bin"
    file_target = fixture.test_root / "rename-file-diagnostic-target.bin"
    file_payload = b"diagnostic-file-sentinel\n"
    file_source.write_bytes(file_payload)
    file_before = core.stable_file(
        file_source,
        max_bytes=core.MAX_JSON_BYTES,
        collect=True,
    )
    file_parent_identity = core.node_identity(os.lstat(fixture.test_root))
    file_error = PermissionError(13, "synthetic access denied", None, 5, None)
    file_rename_call_count = 0

    def file_access_denied_rename(
        observed_source: Any,
        observed_target: Any,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        nonlocal file_rename_call_count
        file_rename_call_count += 1
        if (
            not core.same_path(pathlib.Path(observed_source), file_source)
            or not core.same_path(pathlib.Path(observed_target), file_target)
            or _args
            or _kwargs
        ):
            raise AssertionError("file rename diagnostic intercepted the wrong endpoints")
        raise file_error

    captured_file_error: Optional[core.BootstrapError] = None
    core.os.rename = file_access_denied_rename
    try:
        core.rename_bound_file(
            file_source,
            file_target,
            expected_identity=file_before["identity"],
            expected_payload=file_payload,
            label="diagnostic bound-file rename",
        )
    except core.BootstrapError as exc:
        captured_file_error = exc
    else:
        raise AssertionError("synthetic file access denial did not fail closed")
    finally:
        core.os.rename = real_rename
    if captured_file_error is None or file_rename_call_count != 1:
        raise AssertionError("file rename diagnostic retried or lost its refusal")
    file_refusal = core.bootstrap_error_document(captured_file_error)
    expected_file_diagnostic = {
        "schema": "oni.local-os-error-diagnostic.v1",
        "operation": "rename",
        "operation_label": "diagnostic bound-file rename",
        "action_outcome": "unknown",
        "native_error": {
            "exception_type": "PermissionError",
            "errno": 13,
            "winerror": 5,
            "filename_present": False,
            "filename2_present": False,
            "filename_fields_redacted": True,
        },
        "source": {
            "path_id_lower": core.path_id(file_source),
            "exists": True,
            "identity": file_before["identity"],
            "kind": "file",
        },
        "target": {
            "path_id_lower": core.path_id(file_target),
            "exists": False,
        },
    }
    if (
        file_refusal.get("schema") != "oni.live-runtime-bootstrap-error.v1"
        or file_refusal.get("status") != "blocked"
        or file_refusal.get("classification") != "infra_fail"
        or file_refusal.get("error")
        != "diagnostic bound-file rename atomic rename failed"
        or file_refusal.get("diagnostic") != expected_file_diagnostic
    ):
        raise AssertionError(f"file rename diagnostic contract drifted: {file_refusal}")
    serialized_file_refusal = json.dumps(
        file_refusal,
        ensure_ascii=False,
        sort_keys=True,
    )
    if (
        file_source.name in serialized_file_refusal
        or file_target.name in serialized_file_refusal
        or "synthetic access denied" in serialized_file_refusal
    ):
        raise AssertionError("file rename diagnostic disclosed a path or native message")
    file_after = core.stable_file(
        file_source,
        max_bytes=core.MAX_JSON_BYTES,
        collect=True,
    )
    if (
        file_after != file_before
        or core.node_identity(os.lstat(fixture.test_root)) != file_parent_identity
        or os.path.lexists(file_target)
    ):
        raise AssertionError("failed file diagnostic rename changed its TestRoot endpoints")
    return {
        "name": "bound_rename_postcheck_rejects_interposed_inode",
        "status": "passed",
        "classification": "toctou_attack_outcome_unknown",
    }


def run_process_query_contract_cases() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    roots = core.Roots(
        "process-contract",
        pathlib.Path("C:/offline-process-contract/oni"),
        pathlib.Path("C:/offline-process-contract/evidence"),
        None,
    )
    args = argparse.Namespace(process_state_fixture=None)
    real_run = core.subprocess.run
    response: dict[str, Any] = {}

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=0,
            stdout=json.dumps(response, separators=(",", ":")),
            stderr="",
        )

    core.subprocess.run = fake_run
    try:
        response = {
            "get_process_succeeded": False,
            "get_cim_succeeded": True,
            "OxygenNotIncluded": 0,
            "candidates": [],
        }
        try:
            core.read_process_counts(args, roots)
        except core.BootstrapError:
            pass
        else:
            raise AssertionError("failed Get-Process query was accepted")
        results.append({"name": "process_get_process_failure_closed", "status": "passed"})

        response = {
            "get_process_succeeded": True,
            "get_cim_succeeded": True,
            "OxygenNotIncluded": 0,
            "candidates": [
                {
                    "ProcessId": 123,
                    "Name": "python.exe",
                    "CommandLine": (
                        'python.exe -c "text mentioning oni_bridge.py but not an argv path"'
                    ),
                }
            ],
        }
        counts = core.read_process_counts(args, roots)
        if counts != {"OxygenNotIncluded": 0, "oni_bridge.py": 0}:
            raise AssertionError("process census self-matched non-path source text")
        results.append({"name": "process_query_no_source_text_self_match", "status": "passed"})

        response["candidates"] = [
            {
                "ProcessId": 124,
                "Name": "pythonw.exe",
                "CommandLine": 'pythonw.exe "C:\\workspace\\bridge\\oni_bridge.py" --once',
            }
        ]
        counts = core.read_process_counts(args, roots)
        if counts != {"OxygenNotIncluded": 0, "oni_bridge.py": 1}:
            raise AssertionError("exact Bridge argv path was not counted")
        results.append({"name": "process_query_exact_bridge_argv_counted", "status": "passed"})

        response["candidates"] = [
            {
                "ProcessId": False,
                "Name": "python.exe",
                "CommandLine": 'python.exe "C:\\workspace\\bridge\\oni_bridge.py"',
            }
        ]
        try:
            core.read_process_counts(args, roots)
        except core.BootstrapError:
            pass
        else:
            raise AssertionError("bool ProcessId bypassed exact integer validation")
        results.append({"name": "process_query_bool_pid_refused", "status": "passed"})
    finally:
        core.subprocess.run = real_run
    return results


def _tree_probe_path_key(path: Any) -> str:
    """Normalize logical and extended Windows paths for fault-injection probes."""

    text = os.fsdecode(os.fspath(path))
    text = os.path.abspath(os.path.normpath(text))
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(text).rstrip("\\/")


def run_tree_io_boundary_cases(
    base: pathlib.Path,
    round_index: int,
) -> list[dict[str, Any]]:
    """Prove tree traversal and byte ceilings fail closed inside TestRoot."""

    results: list[dict[str, Any]] = []

    reparse_root = base / f"r{round_index}-tree-reparse"
    reparse_child = reparse_root / "reparse-branch"
    reparse_grandchild = reparse_child / "untrusted-descendant.bin"
    reparse_child.mkdir(parents=True)
    reparse_grandchild.write_bytes(b"fixture-only\n")
    reparse_child_identity = tuple(core.node_identity(os.lstat(reparse_child)))
    reparse_root_key = _tree_probe_path_key(reparse_root)
    reparse_child_key = _tree_probe_path_key(reparse_child)
    real_scandir = core.os.scandir
    real_is_reparse_metadata = core.is_reparse_metadata
    observed_scans: list[str] = []

    def injected_reparse_metadata(metadata: os.stat_result) -> bool:
        return (
            tuple(core.node_identity(metadata)) == reparse_child_identity
            or real_is_reparse_metadata(metadata)
        )

    def reparse_guarded_scandir(path: Any = ".") -> Any:
        key = _tree_probe_path_key(path)
        observed_scans.append(key)
        if key == reparse_child_key:
            raise AssertionError("tree walker descended through an injected reparse node")
        return real_scandir(path)

    core.is_reparse_metadata = injected_reparse_metadata
    core.os.scandir = reparse_guarded_scandir
    try:
        for action, name in (
            (
                core.snapshot_tree,
                "snapshot_reparse_rejected_before_descendant_scandir",
            ),
            (
                core.capture_bound_tree,
                "capture_reparse_rejected_before_descendant_scandir",
            ),
        ):
            observed_scans.clear()
            try:
                action(reparse_root)
            except core.BootstrapError:
                pass
            else:
                raise AssertionError(f"{name} accepted an injected reparse directory")
            if observed_scans != [reparse_root_key]:
                raise AssertionError(
                    f"{name} scandir order crossed the reparse boundary: {observed_scans}"
                )
            results.append({"name": name, "status": "passed"})
    finally:
        core.os.scandir = real_scandir
        core.is_reparse_metadata = real_is_reparse_metadata

    scandir_root = base / f"r{round_index}-deep-scandir-error"
    scandir_deep = scandir_root / "level-a" / "level-b"
    scandir_deep.mkdir(parents=True)
    (scandir_deep / "unreadable.bin").write_bytes(b"fixture-only\n")
    scandir_deep_key = _tree_probe_path_key(scandir_deep)
    observed_scans = []

    def failing_deep_scandir(path: Any = ".") -> Any:
        key = _tree_probe_path_key(path)
        observed_scans.append(key)
        if key == scandir_deep_key:
            raise PermissionError(13, "injected deep scandir refusal", os.fspath(path))
        return real_scandir(path)

    core.os.scandir = failing_deep_scandir
    try:
        for action, name in (
            (core.snapshot_tree, "snapshot_deep_scandir_oserror_fails_closed"),
            (core.capture_bound_tree, "capture_deep_scandir_oserror_fails_closed"),
        ):
            observed_scans.clear()
            captured_error: Optional[core.BootstrapError] = None
            try:
                action(scandir_root)
            except core.BootstrapError as exc:
                captured_error = exc
            if (
                captured_error is None
                or not isinstance(captured_error.__cause__, PermissionError)
                or scandir_deep_key not in observed_scans
            ):
                raise AssertionError(
                    f"{name} swallowed or misclassified the injected scandir OSError"
                )
            results.append({"name": name, "status": "passed"})
    finally:
        core.os.scandir = real_scandir

    iterator_root = base / f"r{round_index}-scandir-iterator-error"
    iterator_deep = iterator_root / "level-a" / "level-b"
    iterator_deep.mkdir(parents=True)
    (iterator_deep / "first.bin").write_bytes(b"first\n")
    (iterator_deep / "second.bin").write_bytes(b"second\n")
    iterator_deep_key = _tree_probe_path_key(iterator_deep)
    iterator_state = {"injected": False}

    class FailingNextScandir:
        def __init__(self, inner: Any, key: str) -> None:
            self.inner = inner
            self.iterator: Any = None
            self.key = key
            self.yielded = 0

        def __enter__(self) -> "FailingNextScandir":
            self.iterator = self.inner.__enter__()
            return self

        def __iter__(self) -> "FailingNextScandir":
            return self

        def __next__(self) -> Any:
            if self.key == iterator_deep_key and self.yielded == 1:
                iterator_state["injected"] = True
                raise PermissionError(13, "injected scandir iterator refusal")
            item = next(self.iterator)
            self.yielded += 1
            return item

        def __exit__(self, *args: Any) -> bool:
            return bool(self.inner.__exit__(*args))

    def iterator_failing_scandir(path: Any = ".") -> FailingNextScandir:
        return FailingNextScandir(real_scandir(path), _tree_probe_path_key(path))

    core.os.scandir = iterator_failing_scandir
    try:
        captured_error = None
        try:
            core.snapshot_tree(iterator_root)
        except core.BootstrapError as exc:
            captured_error = exc
        if (
            captured_error is None
            or not isinstance(captured_error.__cause__, PermissionError)
            or not iterator_state["injected"]
        ):
            raise AssertionError("scandir iterator OSError was swallowed or misclassified")
        results.append(
            {"name": "tree_scandir_iterator_oserror_fails_closed", "status": "passed"}
        )
    finally:
        core.os.scandir = real_scandir

    entry_root = base / f"r{round_index}-direntry-name-error"
    entry_deep = entry_root / "level-a" / "level-b"
    entry_deep.mkdir(parents=True)
    (entry_deep / "unreadable-name.bin").write_bytes(b"fixture-only\n")
    entry_deep_key = _tree_probe_path_key(entry_deep)
    entry_state = {"injected": False}

    class FailingNameEntry:
        @property
        def name(self) -> str:
            entry_state["injected"] = True
            raise PermissionError(13, "injected DirEntry.name refusal")

    class FailingNameScandir:
        def __init__(self, inner: Any, key: str) -> None:
            self.inner = inner
            self.iterator: Any = None
            self.key = key
            self.replaced = False

        def __enter__(self) -> "FailingNameScandir":
            self.iterator = self.inner.__enter__()
            return self

        def __iter__(self) -> "FailingNameScandir":
            return self

        def __next__(self) -> Any:
            item = next(self.iterator)
            if self.key == entry_deep_key and not self.replaced:
                self.replaced = True
                return FailingNameEntry()
            return item

        def __exit__(self, *args: Any) -> bool:
            return bool(self.inner.__exit__(*args))

    def entry_failing_scandir(path: Any = ".") -> FailingNameScandir:
        return FailingNameScandir(real_scandir(path), _tree_probe_path_key(path))

    core.os.scandir = entry_failing_scandir
    try:
        captured_error = None
        try:
            core.capture_bound_tree(entry_root)
        except core.BootstrapError as exc:
            captured_error = exc
        if (
            captured_error is None
            or not isinstance(captured_error.__cause__, PermissionError)
            or not entry_state["injected"]
        ):
            raise AssertionError("DirEntry.name OSError was swallowed or misclassified")
        results.append(
            {"name": "tree_direntry_name_oserror_fails_closed", "status": "passed"}
        )
    finally:
        core.os.scandir = real_scandir

    ceiling_root = base / f"r{round_index}-tree-node-ceiling"
    ceiling_root.mkdir()
    (ceiling_root / "first.bin").write_bytes(b"first\n")
    (ceiling_root / "second.bin").write_bytes(b"second\n")
    real_tree_file_limit = core.MAX_TREE_FILES
    core.MAX_TREE_FILES = 1
    try:
        captured_error = None
        try:
            core._logical_tree_paths(ceiling_root)
        except core.BootstrapError as exc:
            captured_error = exc
        if captured_error is None or str(captured_error) != "bootstrap tree exceeds the safety bound":
            raise AssertionError("tree node ceiling was not enforced during enumeration")
        results.append(
            {"name": "tree_enumeration_node_ceiling_fails_closed", "status": "passed"}
        )
    finally:
        core.MAX_TREE_FILES = real_tree_file_limit

    def run_scandir_exit_identity_swap(label: str, *, deep: bool) -> None:
        swap_root = base / f"r{round_index}-{label}"
        swap_target = swap_root
        if deep:
            swap_target = swap_root / "level-a" / "level-b"
        swap_target.mkdir(parents=True)
        (swap_target / "same-content.bin").write_bytes(b"same-content\n")
        replacement = base / f"r{round_index}-{label}-replacement"
        held = base / f"r{round_index}-{label}-held"
        shutil.copytree(swap_target, replacement, copy_function=shutil.copy2)
        original_identity = tuple(core.node_identity(os.lstat(swap_target)))
        target_key = _tree_probe_path_key(swap_target)
        swap_state = {"done": False}

        class SwapOnExitIterator:
            def __init__(self, inner: Any, key: str) -> None:
                self.inner = inner
                self.iterator: Any = None
                self.key = key

            def __enter__(self) -> "SwapOnExitIterator":
                self.iterator = self.inner.__enter__()
                return self

            def __iter__(self) -> "SwapOnExitIterator":
                return self

            def __next__(self) -> Any:
                return next(self.iterator)

            def __exit__(self, *args: Any) -> bool:
                outcome = bool(self.inner.__exit__(*args))
                if self.key == target_key and not swap_state["done"]:
                    swap_target.rename(held)
                    replacement.rename(swap_target)
                    swap_state["done"] = True
                return outcome

        def swapping_scandir(path: Any = ".") -> SwapOnExitIterator:
            return SwapOnExitIterator(real_scandir(path), _tree_probe_path_key(path))

        core.os.scandir = swapping_scandir
        try:
            captured_error: Optional[core.BootstrapError] = None
            try:
                core._logical_tree_paths(swap_root)
            except core.BootstrapError as exc:
                captured_error = exc
            replacement_identity = tuple(core.node_identity(os.lstat(swap_target)))
            if (
                captured_error is None
                or not swap_state["done"]
                or replacement_identity == original_identity
            ):
                raise AssertionError(
                    f"{label} did not reject a byte-identical directory identity swap"
                )
            results.append({"name": label, "status": "passed"})
        finally:
            core.os.scandir = real_scandir

    run_scandir_exit_identity_swap(
        "tree_root_identity_swap_on_scandir_exit_fails_closed",
        deep=False,
    )
    run_scandir_exit_identity_swap(
        "tree_deep_identity_swap_on_scandir_exit_fails_closed",
        deep=True,
    )

    cycle_root = base / f"r{round_index}-duplicate-directory-identity"
    cycle_child = cycle_root / "cycle-child"
    cycle_child.mkdir(parents=True)
    (cycle_child / "untrusted-descendant.bin").write_bytes(b"fixture-only\n")
    real_node_identity = core.node_identity
    cycle_root_identity = tuple(real_node_identity(os.lstat(cycle_root)))
    cycle_child_identity = tuple(real_node_identity(os.lstat(cycle_child)))
    cycle_root_key = _tree_probe_path_key(cycle_root)
    cycle_child_key = _tree_probe_path_key(cycle_child)
    cycle_scans: list[str] = []

    def duplicate_directory_identity(metadata: os.stat_result) -> list[int]:
        identity = tuple(real_node_identity(metadata))
        if identity == cycle_child_identity:
            return list(cycle_root_identity)
        return list(identity)

    def cycle_guarded_scandir(path: Any = ".") -> Any:
        key = _tree_probe_path_key(path)
        cycle_scans.append(key)
        if key == cycle_child_key:
            raise AssertionError("tree walker scanned a duplicate directory identity")
        return real_scandir(path)

    core.node_identity = duplicate_directory_identity
    core.os.scandir = cycle_guarded_scandir
    try:
        captured_error = None
        try:
            core._logical_tree_paths(cycle_root)
        except core.BootstrapError as exc:
            captured_error = exc
        if captured_error is None or cycle_scans != [cycle_root_key]:
            raise AssertionError(
                f"duplicate directory identity was not rejected before descent: {cycle_scans}"
            )
        results.append(
            {"name": "tree_duplicate_directory_identity_fails_closed", "status": "passed"}
        )
    finally:
        core.os.scandir = real_scandir
        core.node_identity = real_node_identity

    stable_root = base / f"r{round_index}-stable-growth"
    stable_root.mkdir()
    stable_path = stable_root / "growing.bin"
    stable_limit = 8
    stable_growth_size = 1024 * 1024
    stable_path.write_bytes(b"x" * stable_limit)
    real_fdopen = core.os.fdopen
    read_observations: list[tuple[int, int]] = []

    class GrowingReadHandle:
        def __init__(self, descriptor: int, *args: Any, **kwargs: Any) -> None:
            self.handle = real_fdopen(descriptor, *args, **kwargs)
            self.read_count = 0

        def __enter__(self) -> "GrowingReadHandle":
            return self

        def __exit__(self, *_args: Any) -> bool:
            self.handle.close()
            return False

        def fileno(self) -> int:
            return self.handle.fileno()

        def read(self, size: int = -1) -> bytes:
            self.read_count += 1
            payload = self.handle.read(size)
            if self.read_count == 1:
                with stable_path.open("ab", buffering=0) as writer:
                    writer.write(b"+" * stable_growth_size)
            elif self.read_count == 2 and not payload:
                # Buffered readers may cache the first EOF observation.  The
                # physical file still grew; return that exact appended byte so
                # the per-chunk ceiling is exercised deterministically.
                payload = b"+"
            read_observations.append((size, len(payload)))
            return payload

    def growing_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> GrowingReadHandle:
        return GrowingReadHandle(descriptor, *args, **kwargs)

    core.os.fdopen = growing_fdopen
    try:
        captured_error = None
        try:
            core.stable_file(stable_path, max_bytes=stable_limit, collect=False)
        except core.BootstrapError as exc:
            captured_error = exc
        if (
            captured_error is None
            or str(captured_error) != "stable file exceeds the size bound"
            or read_observations != [(stable_limit + 1, stable_limit), (1, 1)]
            or sum(length for _request, length in read_observations) > stable_limit + 1
            or stable_path.stat().st_size != stable_limit + stable_growth_size
        ):
            raise AssertionError(
                "stable_file did not enforce max_bytes immediately after the growth chunk"
            )
        results.append(
            {"name": "stable_file_growth_chunk_enforces_max_bytes", "status": "passed"}
        )
    finally:
        core.os.fdopen = real_fdopen

    empty_path = stable_root / "empty.bin"
    empty_path.write_bytes(b"")
    empty_state = core.stable_file(empty_path, max_bytes=0, collect=True)
    if empty_state.get("length") != 0 or empty_state.get("bytes") != b"":
        raise AssertionError("stable_file rejected an empty file at the zero-byte boundary")
    results.append({"name": "stable_file_zero_byte_boundary_passes", "status": "passed"})

    zero_growth_path = stable_root / "zero-growth.bin"
    zero_growth_path.write_bytes(b"")
    zero_growth_reads: list[tuple[int, int]] = []

    class ZeroGrowthHandle:
        def __init__(self, descriptor: int, *args: Any, **kwargs: Any) -> None:
            self.handle = real_fdopen(descriptor, *args, **kwargs)

        def __enter__(self) -> "ZeroGrowthHandle":
            return self

        def __exit__(self, *_args: Any) -> bool:
            self.handle.close()
            return False

        def fileno(self) -> int:
            return self.handle.fileno()

        def read(self, size: int = -1) -> bytes:
            payload = self.handle.read(size)
            if not zero_growth_reads:
                with zero_growth_path.open("ab", buffering=0) as writer:
                    writer.write(b"+")
                if not payload:
                    payload = b"+"
            zero_growth_reads.append((size, len(payload)))
            return payload

    def zero_growth_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> ZeroGrowthHandle:
        return ZeroGrowthHandle(descriptor, *args, **kwargs)

    core.os.fdopen = zero_growth_fdopen
    try:
        captured_error = None
        try:
            core.stable_file(zero_growth_path, max_bytes=0, collect=False)
        except core.BootstrapError as exc:
            captured_error = exc
        if (
            captured_error is None
            or str(captured_error) != "stable file exceeds the size bound"
            or zero_growth_reads != [(1, 1)]
            or zero_growth_path.stat().st_size != 1
        ):
            raise AssertionError(
                "stable_file did not fail closed on growth from the zero-byte boundary"
            )
        results.append(
            {"name": "stable_file_zero_byte_growth_fails_closed", "status": "passed"}
        )
    finally:
        core.os.fdopen = real_fdopen

    real_tree_limit = core.MAX_TREE_BYTES
    real_stable_file = core.stable_file

    snapshot_root = base / f"r{round_index}-snapshot-budget"
    snapshot_root.mkdir()
    (snapshot_root / "a.bin").write_bytes(b"a" * 6)
    (snapshot_root / "b.bin").write_bytes(b"b" * 4)
    snapshot_calls: list[tuple[str, int]] = []

    def observe_snapshot_budget(
        path: pathlib.Path,
        *,
        max_bytes: int,
        collect: bool = True,
    ) -> dict[str, Any]:
        snapshot_calls.append((path.name, max_bytes))
        return real_stable_file(path, max_bytes=max_bytes, collect=collect)

    core.MAX_TREE_BYTES = 10
    core.stable_file = observe_snapshot_budget
    try:
        snapshot = core.snapshot_tree(snapshot_root)
        if snapshot.get("total_bytes") != 10 or snapshot_calls != [
            ("a.bin", 10),
            ("b.bin", 4),
        ]:
            raise AssertionError(
                f"snapshot_tree did not spend the aggregate budget monotonically: {snapshot_calls}"
            )
        results.append(
            {"name": "snapshot_tree_passes_remaining_byte_budget", "status": "passed"}
        )
    finally:
        core.stable_file = real_stable_file
        core.MAX_TREE_BYTES = real_tree_limit

    snapshot_exhausted_root = base / f"r{round_index}-snapshot-exhausted-budget"
    snapshot_exhausted_root.mkdir()
    (snapshot_exhausted_root / "a.bin").write_bytes(b"a" * 10)
    (snapshot_exhausted_root / "b.bin").write_bytes(b"b")
    snapshot_exhausted_calls: list[tuple[str, int]] = []

    def observe_snapshot_exhaustion(
        path: pathlib.Path,
        *,
        max_bytes: int,
        collect: bool = True,
    ) -> dict[str, Any]:
        snapshot_exhausted_calls.append((path.name, max_bytes))
        return real_stable_file(path, max_bytes=max_bytes, collect=collect)

    core.MAX_TREE_BYTES = 10
    core.stable_file = observe_snapshot_exhaustion
    try:
        captured_error = None
        try:
            core.snapshot_tree(snapshot_exhausted_root)
        except core.BootstrapError as exc:
            captured_error = exc
        if (
            captured_error is None
            or str(captured_error) != "stable file exceeds the size bound"
            or snapshot_exhausted_calls != [("a.bin", 10), ("b.bin", 0)]
        ):
            raise AssertionError(
                "snapshot_tree did not reject the first byte after aggregate exhaustion"
            )
        results.append(
            {"name": "snapshot_tree_exhausted_budget_rejects_next_byte", "status": "passed"}
        )
    finally:
        core.stable_file = real_stable_file
        core.MAX_TREE_BYTES = real_tree_limit

    capture_root = base / f"r{round_index}-capture-budget"
    capture_root.mkdir()
    (capture_root / "a.bin").write_bytes(b"a" * 6)
    (capture_root / "b.bin").write_bytes(b"b" * 4)
    capture_calls: list[tuple[str, int]] = []

    def observe_capture_budget(
        path: pathlib.Path,
        *,
        max_bytes: int,
        collect: bool = True,
    ) -> dict[str, Any]:
        capture_calls.append((path.name, max_bytes))
        return real_stable_file(path, max_bytes=max_bytes, collect=collect)

    core.MAX_TREE_BYTES = 10
    core.stable_file = observe_capture_budget
    try:
        capture_snapshot, _capture_identity = core.capture_bound_tree(capture_root)
        if capture_snapshot.get("total_bytes") != 10 or capture_calls != [
            ("a.bin", 10),
            ("b.bin", 4),
            ("a.bin", 6),
            ("b.bin", 4),
        ]:
            raise AssertionError(
                f"capture_bound_tree byte ceilings drifted across its sandwich: {capture_calls}"
            )
        results.append(
            {"name": "capture_tree_passes_remaining_and_exact_byte_budgets", "status": "passed"}
        )
    finally:
        core.stable_file = real_stable_file
        core.MAX_TREE_BYTES = real_tree_limit

    capture_exhausted_root = base / f"r{round_index}-capture-exhausted-budget"
    capture_exhausted_root.mkdir()
    (capture_exhausted_root / "a.bin").write_bytes(b"a" * 10)
    (capture_exhausted_root / "b.bin").write_bytes(b"b")
    capture_exhausted_calls: list[tuple[str, int]] = []

    def observe_capture_exhaustion(
        path: pathlib.Path,
        *,
        max_bytes: int,
        collect: bool = True,
    ) -> dict[str, Any]:
        capture_exhausted_calls.append((path.name, max_bytes))
        return real_stable_file(path, max_bytes=max_bytes, collect=collect)

    core.MAX_TREE_BYTES = 10
    core.stable_file = observe_capture_exhaustion
    try:
        captured_error = None
        try:
            core.capture_bound_tree(capture_exhausted_root)
        except core.BootstrapError as exc:
            captured_error = exc
        if (
            captured_error is None
            or str(captured_error) != "stable file exceeds the size bound"
            or capture_exhausted_calls != [("a.bin", 10), ("b.bin", 0)]
        ):
            raise AssertionError(
                "capture_bound_tree did not reject the first byte after aggregate exhaustion"
            )
        results.append(
            {"name": "capture_tree_exhausted_budget_rejects_next_byte", "status": "passed"}
        )
    finally:
        core.stable_file = real_stable_file
        core.MAX_TREE_BYTES = real_tree_limit

    second_pass_root = base / f"r{round_index}-capture-second-growth"
    second_pass_root.mkdir()
    second_pass_file = second_pass_root / "payload.bin"
    second_pass_file.write_bytes(b"data")
    second_pass_calls: list[tuple[str, int]] = []

    def grow_between_capture_passes(
        path: pathlib.Path,
        *,
        max_bytes: int,
        collect: bool = True,
    ) -> dict[str, Any]:
        second_pass_calls.append((path.name, max_bytes))
        record = real_stable_file(path, max_bytes=max_bytes, collect=collect)
        if len(second_pass_calls) == 1:
            with second_pass_file.open("ab", buffering=0) as writer:
                writer.write(b"+")
        return record

    core.MAX_TREE_BYTES = 10
    core.stable_file = grow_between_capture_passes
    try:
        captured_error = None
        try:
            core.capture_bound_tree(second_pass_root)
        except core.BootstrapError as exc:
            captured_error = exc
        if (
            captured_error is None
            or str(captured_error) != "stable file exceeds the size bound"
            or second_pass_calls != [("payload.bin", 10), ("payload.bin", 4)]
            or second_pass_file.stat().st_size != 5
        ):
            raise AssertionError(
                "capture_bound_tree did not cap its second file read at the first-pass length"
            )
        results.append(
            {"name": "capture_second_pass_growth_hits_exact_byte_bound", "status": "passed"}
        )
    finally:
        core.stable_file = real_stable_file
        core.MAX_TREE_BYTES = real_tree_limit

    return results


def run_adversarial_cases(base: pathlib.Path, round_index: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    # Canonical plan attack with every dependent self-hash resealed.
    fixture = make_fixture(base, f"r{round_index}-plan-reseal")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_journal_initialized")
    if interrupted.returncode != 99:
        raise AssertionError("plan reseal fixture did not latch")
    reseal_plan_attack(fixture)
    results.append(expect_blocked(fixture, "canonical_plan_reseal_refused"))

    # Journal grammar attack with a valid new self-hash.
    fixture = make_fixture(base, f"r{round_index}-journal-reseal")
    invoke(fixture, apply=True, fail_at="after_probe_stage_journal")
    journal_file = sorted((transaction_dir(fixture) / "journal").glob("*.json"))[-1]
    entry = json.loads(journal_file.read_text(encoding="utf-8"))
    entry["data"]["extra"] = False
    unsigned = dict(entry)
    unsigned.pop("entry_hash", None)
    entry["entry_hash"] = core.canonical_hash(unsigned)
    write_json(journal_file, entry)
    results.append(expect_blocked(fixture, "journal_extra_reseal_refused"))

    # Result values cannot be forged by consistently rehashing the document.
    fixture = make_fixture(base, f"r{round_index}-result-reseal")
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 0:
        raise AssertionError(value)
    result_path = transaction_dir(fixture) / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["release_dll_sha256"] = "sha256:" + "f" * 64
    unsigned = dict(result)
    unsigned.pop("result_hash", None)
    result["result_hash"] = core.canonical_hash(unsigned)
    write_json(result_path, result)
    results.append(expect_blocked(fixture, "result_reseal_refused"))

    # Workspace drift before any ONI mutation is fail-closed.
    fixture = make_fixture(base, f"r{round_index}-workspace-drift")
    invoke(fixture, apply=True, fail_at="after_bridge_stage_journal")
    (fixture.release / "OniAiProbeMod.dll").write_bytes(b"drifted-workspace")
    results.append(expect_blocked(fixture, "workspace_drift_before_commit_refused"))

    # Once Klei mutation began, immutable frozen stage custody is sufficient to
    # roll forward even if the workspace source changes.
    fixture = make_fixture(base, f"r{round_index}-frozen-rollforward")
    interrupted, _ = invoke(fixture, apply=True, fail_at="after_old_bridge_physical")
    if interrupted.returncode != 99:
        raise AssertionError("frozen rollforward fixture did not reach partial live state")
    (fixture.release / "OniAiProbeMod.dll").write_bytes(b"post-commit-source-drift")
    resumed, resumed_json = invoke(fixture, apply=True)
    if resumed.returncode != 0:
        raise AssertionError(f"frozen source rollforward failed: {resumed_json}")
    assert_installed(fixture, resumed_json)
    results.append({"name": "frozen_source_rollforward", "status": "passed"})

    # Manifest name-swap with byte-identical content is rejected by inode.
    fixture = make_fixture(base, f"r{round_index}-manifest-swap")
    invoke(fixture, apply=True, fail_at="after_journal_initialized")
    held = fixture.manifest.with_suffix(".held")
    fixture.manifest.rename(held)
    shutil.copy2(held, fixture.manifest)
    results.append(expect_blocked(fixture, "environment_manifest_name_swap_refused"))

    # Stage name-swap with byte-identical content is rejected by frozen identity.
    fixture = make_fixture(base, f"r{round_index}-stage-swap")
    invoke(fixture, apply=True, fail_at="after_probe_stage_journal")
    probe_stage = transaction_dir(fixture) / "stage" / "probe"
    held_stage = probe_stage.with_name("probe-held")
    probe_stage.rename(held_stage)
    shutil.copytree(held_stage, probe_stage, copy_function=shutil.copy2)
    results.append(expect_blocked(fixture, "staged_probe_name_swap_refused"))

    # Current old Bridge name-swap is rejected before the first Klei mutation.
    fixture = make_fixture(base, f"r{round_index}-old-bridge-swap")
    invoke(fixture, apply=True, fail_at="before_old_bridge_physical")
    old_bridge = fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts)
    held_bridge = old_bridge.with_name("ai-bridge-held")
    old_bridge.rename(held_bridge)
    shutil.copytree(held_bridge, old_bridge, copy_function=shutil.copy2)
    results.append(expect_blocked(fixture, "old_bridge_name_swap_refused"))

    # Deterministic archive collision is retained; nothing external is removed.
    fixture = make_fixture(base, f"r{round_index}-archive-collision")
    invoke(fixture, apply=True, fail_at="after_bridge_stage_journal")
    plan = json.loads((transaction_dir(fixture) / "plan.json").read_text(encoding="utf-8"))
    archive = fixture.oni / pathlib.Path(
        *pathlib.PurePosixPath(plan["desired"]["archive_relative"]).parts
    )
    archive.mkdir(parents=True)
    marker = archive / "foreign.txt"
    marker.write_text("external\n", encoding="utf-8")
    results.append(expect_blocked(fixture, "archive_collision_refused_and_retained"))
    if marker.read_text(encoding="utf-8") != "external\n":
        raise AssertionError("archive collision was overwritten/deleted")

    # Lock hardlink is rejected before advisory lock acquisition.
    fixture = make_fixture(base, f"r{round_index}-lock-hardlink")
    invoke(fixture, apply=True, fail_at="after_journal_initialized")
    lock = transaction_dir(fixture) / "transaction.lock"
    lock_alias_token = _create_fixture_hardlink_alias(
        fixture.test_root,
        lock,
        transaction_dir(fixture) / "foreign-lock-link",
    )
    lock_result = expect_blocked(fixture, "hardlinked_transaction_lock_refused")
    _release_fixture_hardlink_alias(lock_alias_token)
    results.append(lock_result)

    # Hard-linked workspace source is rejected before publication.
    fixture = make_fixture(base, f"r{round_index}-release-hardlink")
    release_dll = fixture.release / "OniAiProbeMod.dll"
    release_alias_token = _create_fixture_hardlink_alias(
        fixture.test_root,
        release_dll,
        fixture.release / "OniAiProbeMod.dll.external-link",
    )
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 2 or transaction_dir(fixture).exists():
        raise AssertionError(f"hardlinked release was not rejected before latch: {value}")
    _release_fixture_hardlink_alias(release_alias_token)
    results.append({"name": "hardlinked_release_refused", "status": "passed"})

    # Archive-root mtime after commit is part of completed custody.
    fixture = make_fixture(base, f"r{round_index}-archive-mtime")
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 0:
        raise AssertionError(value)
    archive_root = fixture.oni / pathlib.Path(*core.ARCHIVE_ROOT_RELATIVE.parts)
    before = os.lstat(archive_root).st_mtime_ns
    os.utime(archive_root, ns=(before + 10_000_000, before + 10_000_000))
    results.append(expect_blocked(fixture, "archive_root_mtime_drift_refused"))

    # Exact integer process guard (bool is not integer) and ordinary non-zero.
    fixture = make_fixture(base, f"r{round_index}-process-running")
    write_json(fixture.process_fixture, {"OxygenNotIncluded": 1, "oni_bridge.py": 0})
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 2 or transaction_dir(fixture).exists():
        raise AssertionError(f"running process did not block before latch: {value}")
    results.append({"name": "initial_process_guard", "status": "passed"})
    fixture = make_fixture(base, f"r{round_index}-process-bool")
    write_json(fixture.process_fixture, {"OxygenNotIncluded": False, "oni_bridge.py": 0})
    completed, value = invoke(fixture, apply=True)
    if completed.returncode != 2 or transaction_dir(fixture).exists():
        raise AssertionError(f"bool process count bypassed exact type guard: {value}")
    results.append({"name": "process_bool_int_confusion_refused", "status": "passed"})

    # TOCTOU process start immediately before first old-Bridge rename.
    fixture = make_fixture(base, f"r{round_index}-process-toctou")
    parsed = parse_apply_args(fixture)

    def process_hook(point: str) -> None:
        if point == "before_old_bridge_physical":
            write_json(fixture.process_fixture, {"OxygenNotIncluded": 1, "oni_bridge.py": 0})

    try:
        core.execute(parsed, hook=process_hook)
    except core.BootstrapError:
        pass
    else:
        raise AssertionError("TOCTOU process start was not blocked")
    if not (fixture.oni / pathlib.Path(*core.BRIDGE_RELATIVE.parts) / "old-state.json").exists():
        raise AssertionError("TOCTOU process guard moved old Bridge")
    results.append({"name": "process_toctou_guard", "status": "passed"})

    return results


def run_static_contract_cases() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    manager_source = MANAGER.read_text(encoding="utf-8")
    wrapper_source = (PROJECT_ROOT / "scripts" / "prepare-v27-runtime.ps1").read_text(
        encoding="utf-8"
    )
    if "dotnet" in manager_source.casefold():
        raise AssertionError("transaction manager contains a build invocation")
    combined_surface = (manager_source + "\n" + wrapper_source).casefold()
    if "oxygennotincluded.exe" in combined_surface or "run-bridge.ps1" in combined_surface:
        raise AssertionError("runtime bootstrap contains a game launch invocation")
    results.append(
        {
            "name": "manager_has_no_build_and_workflow_has_no_game_launch_surface",
            "status": "passed",
        }
    )

    self_command = (
        'powershell.exe -NoProfile -Command "select python candidates; literal oni_bridge.py text"'
    )
    argv = core.windows_command_line_argv(self_command)
    if any(pathlib.PureWindowsPath(item).name.casefold() == "oni_bridge.py" for item in argv):
        raise AssertionError("PowerShell census command self-matches as Bridge")
    positive = core.windows_command_line_argv(
        'python.exe "C:\\workspace\\bridge\\oni_bridge.py" --once'
    )
    if not any(pathlib.PureWindowsPath(item).name.casefold() == "oni_bridge.py" for item in positive):
        raise AssertionError("exact Bridge script argv was not recognized")
    results.append({"name": "process_census_no_self_match", "status": "passed"})
    results.extend(run_process_query_contract_cases())

    if core.utf16_units("") != 0 or core.utf16_units("x") != 1:
        raise AssertionError("UTF-16 path budget does not preserve the 0/1-unit boundary")
    results.append({"name": "legacy_path_0_and_1_unit_boundaries", "status": "passed"})

    # Reproduce the production worker's 78-unit nested TestRoot without
    # creating anything.  The former verifier-only marker reached 261 units
    # here even though every production path passed its 259-unit preflight.
    regression_base = pathlib.Path("C:/") / ("x" * 75)
    regression_candidates = _ordinary_fixture_path_budget_candidates(
        regression_base
    )
    injected_marker = regression_candidates[-1]
    former_marker = injected_marker.with_name("external-duplicate.bin")
    if core.utf16_units(former_marker) != 261:
        raise AssertionError("fixture marker regression no longer reproduces 261 units")
    if core.utf16_units(injected_marker) > 259:
        raise AssertionError("short fixture marker exceeds the legacy path budget")
    assert_actual_fixture_path_budget(regression_base)
    results.append(
        {
            "name": "verifier_injected_marker_legacy_path_regression",
            "status": "passed",
        }
    )

    # Exercise the exact 259/260 UTF-16-unit boundary without asking Windows to
    # create an over-budget path.
    archive_name = "run-preexisting-rtb-0123456789abcdef01234567"
    synthetic_plan = {
        "transaction_id": "rtb-0123456789abcdef01234567",
        "desired": {
            "bootstrap": {"bridge_dir": str(pathlib.Path("C:/x/ai-bridge"))},
            "archive_relative": f"ai-bridge-archives/{archive_name}",
        },
        "prestate": {
            "old_bridge": {"snapshot": {"entries": [{"relative": "old-state.json"}]}},
            "probe": {"snapshot": {"entries": [{"relative": "legacy.dll"}]}},
        },
    }
    base = pathlib.Path("C:/")
    observed_budget_paths: list[str] = []
    real_utf16_units = core.utf16_units

    def capture_budget_path(path: pathlib.Path) -> int:
        observed_budget_paths.append(str(path).replace("\\", "/").casefold())
        return 0

    synthetic_roots = core.Roots("r", base / "oni", base / "evidence", None)
    synthetic_abort_plan, synthetic_explicit = _maximum_path_budget_abort_contract(
        synthetic_roots,
        archive_name,
    )
    core.utf16_units = capture_budget_path
    try:
        core.validate_legacy_path_budget(
            synthetic_roots,
            synthetic_plan,
            synthetic_abort_plan,
            synthetic_explicit,
        )
    finally:
        core.utf16_units = real_utf16_units
    required_budget_fragments = {
        "environment/runtime-bootstrap-install-stage.json",
        "environment/runtime-bootstrap-install-stage.json.new",
        "environment/runtime-bootstrap-abort-stage.json",
        "environment/runtime-bootstrap-abort-stage.json.new",
        "environment/runtime-bootstrap-initialization-outcome-unknown.json",
        "environment/runtime-bootstrap-initialization-outcome-unknown.json.new",
        "runtime-bootstrap-init-00.json",
        "runtime-bootstrap-init-00.json.new",
        "runtime-bootstrap/result-candidate.json",
        "runtime-bootstrap/result-candidate.json.new",
        f"runtime-bootstrap/abort/custody/a/{archive_name}/old-state.json",
        "runtime-bootstrap/abort/custody/p/legacy.dll",
        "runtime-bootstrap/abort/journal/0010-probe_container_mtime_restored_claim_prepared.json",
        "runtime-bootstrap/abort/journal/0010-probe_container_mtime_restored_claim_prepared.json.new",
        "environment-lock-attempts/0000-install/owner.json",
        "environment-lock-attempts/0000-install/owner.json.new",
        "environment-lock-attempts/0000-install/lock.stage.new",
        "environment-lock-attempts/0000-install/claim.json.new",
        "environment-lock-attempts/0000-install/acquired.json.new",
        "environment-lock-attempts/0000-install/released.lock",
        "environment-lock-attempts/0000-install/released.json.new",
        "environment-lock-attempts/0001-replay/released.json.new",
        "environment-lock-attempts/0002-abort/owner.json",
    }
    missing_budget_fragments = sorted(
        fragment
        for fragment in required_budget_fragments
        if not any(fragment in candidate for candidate in observed_budget_paths)
    )
    if missing_budget_fragments:
        raise AssertionError(
            "legacy path budget omitted transient/runtime paths: "
            + ", ".join(missing_budget_fragments)
        )
    boundary_name = "x" * MAX_FIXTURE_BASENAME_UTF16_UNITS
    if fixture_basename(boundary_name) != boundary_name:
        raise AssertionError("20-unit fixture basename was unexpectedly compacted")
    compacted_boundary = fixture_basename(boundary_name + "x")
    if len(compacted_boundary.encode("utf-16-le")) // 2 != 20:
        raise AssertionError("21-unit fixture basename was not compacted to 20 units")
    known_overflow_name = "r1-abort-gate-8f6325b5da"
    if fixture_basename(known_overflow_name) != "r1-abort--95d168fbf3":
        raise AssertionError("known 260-unit fixture basename did not compact deterministically")
    results.append(
        {
            "name": "legacy_path_budget_enumerates_all_transaction_transients",
            "status": "passed",
        }
    )

    chosen: Optional[tuple[core.Roots, int]] = None
    for size in range(1, 220):
        evidence = base / ("e" * size)
        roots = core.Roots("r", base / "oni", evidence, None)
        synthetic_abort_plan, synthetic_explicit = (
            _maximum_path_budget_abort_contract(roots, archive_name)
        )
        try:
            core.validate_legacy_path_budget(
                roots,
                synthetic_plan,
                synthetic_abort_plan,
                synthetic_explicit,
            )
        except core.BootstrapError:
            continue
        longest = max(
            core.utf16_units(path)
            for path in core.legacy_path_candidates(
                roots,
                synthetic_plan,
                synthetic_abort_plan,
                synthetic_explicit,
            )
        )
        if longest == 259:
            chosen = (roots, size)
            break
    if chosen is None:
        raise AssertionError("could not synthesize the 259-unit path budget boundary")
    roots_259, size = chosen
    synthetic_abort_plan, synthetic_explicit = _maximum_path_budget_abort_contract(
        roots_259,
        archive_name,
    )
    core.validate_legacy_path_budget(
        roots_259,
        synthetic_plan,
        synthetic_abort_plan,
        synthetic_explicit,
    )
    roots_260 = core.Roots("r", base / "oni", base / ("e" * (size + 1)), None)
    synthetic_abort_plan, synthetic_explicit = _maximum_path_budget_abort_contract(
        roots_260,
        archive_name,
    )
    try:
        core.validate_legacy_path_budget(
            roots_260,
            synthetic_plan,
            synthetic_abort_plan,
            synthetic_explicit,
        )
    except core.BootstrapError:
        pass
    else:
        raise AssertionError("260-unit legacy path was not rejected")
    results.append({"name": "legacy_path_259_pass_260_fail", "status": "passed"})
    return results


def run_round(base: pathlib.Path, round_index: int) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []

    batch_index = 0

    def _run_batched(action, *action_args, **action_kwargs):
        nonlocal batch_index
        batch_name = _batch_root_name(batch_index)
        batch_index += 1
        return _run_batch(base, batch_name, action, *action_args, **action_kwargs)

    def _append_scenarios(result: Any) -> None:
        if isinstance(result, list):
            scenarios.extend(result)
        else:
            scenarios.append(result)

    _append_scenarios(_run_batched(run_what_if, round_index))
    _append_scenarios(
        _run_batched(run_windows_long_archive_what_if, round_index)
    )
    _append_scenarios(_run_batched(run_tree_io_boundary_cases, round_index))
    _append_scenarios(_run_batched(run_stage_authorization_cases, round_index))
    _append_scenarios(_run_batched(run_initialization_outcome_unknown_cases, round_index))
    _append_scenarios(_run_batched(run_malformed_committed_journal_classification, round_index))
    _append_scenarios(_run_batched(run_abort_happy_path_and_replay, round_index))
    _append_scenarios(_run_batched(run_abort_stage_pre_return_recovery, round_index))
    _append_scenarios(_run_batched(run_abort_exact_stage_preplant, round_index))
    _append_scenarios(_run_batched(run_abort_explicit_owner_attacks, round_index))
    _append_scenarios(_run_batched(run_abort_save_inventory_attacks, round_index))
    _append_scenarios(_run_batched(run_abort_preconfig_gate_attacks, round_index))
    _append_scenarios(_run_batched(run_abort_requires_installed_ready_cases, round_index))
    _append_scenarios(_run_batched(run_abort_anchor_attacks, round_index))
    for point in ABORT_FAULT_POINTS:
        _append_scenarios(
            _run_batched(run_abort_fault_case, point, round_index)
        )
    _append_scenarios(_run_batched(run_abort_attempt_parent_claim_attacks, round_index))
    _append_scenarios(_run_batched(run_abort_transformed_archive_custody_cases, round_index))
    for point in ABORT_ARCHIVE_MTIME_FAULT_POINTS:
        _append_scenarios(
            _run_batched(
                run_abort_fault_case,
                point,
                round_index,
                archive_present=True,
            )
        )
    for point in ABORT_READY_LOCK_SECOND_OCCURRENCE_FAULT_POINTS:
        _append_scenarios(
            _run_batched(run_abort_ready_lock_fault_case, point, round_index)
        )
    _append_scenarios(_run_batched(run_abort_process_toctou_cases, round_index))
    _append_scenarios(_run_batched(run_abort_unfinished_lock_classification_cases, round_index))
    _append_scenarios(_run_batched(run_abort_external_replacement_latches, round_index))
    _append_scenarios(_run_batched(run_abort_missing_root_latch_cases, round_index))
    _append_scenarios(_run_batched(run_support_preclaim_attacks, round_index))
    _append_scenarios(_run_batched(run_presence_cases, round_index))
    for point in FAULT_POINTS:
        _append_scenarios(_run_batched(run_fault_case, point, round_index))
    _append_scenarios(_run_batched(run_ready_publication_interposition, round_index))
    _append_scenarios(_run_batched(run_ready_release_suffix_recovery, round_index))
    _append_scenarios(_run_batched(run_completed_replay_convergence, round_index))
    _append_scenarios(_run_batched(run_typed_exact_cases, round_index))
    _append_scenarios(_run_batched(run_journal_attack_cases, round_index))
    _append_scenarios(_run_batched(run_bound_rename_race_case, round_index))
    _append_scenarios(_run_batched(run_adversarial_cases, round_index))
    return {
        "round": round_index,
        "status": "passed",
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args()
    if args.rounds < 2:
        raise SystemExit("at least two full rounds are required")
    static = run_static_contract_cases()
    rounds: list[dict[str, Any]] = []
    failure: Optional[str] = None
    preserved_test_root: Optional[str] = None
    started = time.time()
    base = pathlib.Path(tempfile.mkdtemp(prefix="oni-rtb-v-"))
    base_metadata, base_kind, base_identity = _cleanup_lstat(base)
    del base_metadata
    if base_kind != "directory":
        raise AssertionError(f"fresh verifier TestRoot is unsafe: {base}")
    base_custody: Optional[_CleanupDirectoryCustody] = None
    try:
        base_custody = _acquire_cleanup_directory_custody(base, base_identity)
        # Ordinary positives stay comfortably under the legacy Win32 path
        # ceiling.  The dedicated MAX_PATH cases below synthesize the exact
        # 259/260 boundary and cover moved-tree descendants separately.
        assert_actual_fixture_path_budget(base)
        for round_index in range(1, args.rounds + 1):
            base_custody.validate()
            rounds.append(run_round(base, round_index))
            base_custody.validate()
    except Exception as exc:  # Keep the report small; traceback remains on stderr.
        failure = f"{type(exc).__name__}: {exc}"
        preserved_test_root = str(base.absolute())
        import traceback

        traceback.print_exc()
    if failure is not None and base_custody is not None:
        base_custody.close()
        base_custody = None
    if failure is None:
        try:
            assert base_custody is not None
            base_custody.validate()
            temp_parent = pathlib.Path(tempfile.gettempdir()).absolute()
            lexical_base = base.absolute()
            if (
                lexical_base.parent != temp_parent
                or not lexical_base.name.startswith("oni-rtb-v-")
            ):
                raise AssertionError(f"unsafe verifier cleanup path: {lexical_base}")
            if _snapshot_direct_children(base):
                raise AssertionError(f"successful verifier TestRoot is not empty: {base}")
            base_custody.close()
            base_custody = None
            _delete_handle_bound_tree(base)
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            preserved_test_root = str(base.absolute())
            import traceback

            traceback.print_exc()
    if base_custody is not None:
        base_custody.close()
    scenario_count = len(static) + sum(item.get("scenario_count", 0) for item in rounds)
    report = {
        "schema": "oni.live-runtime-bootstrap-verification.v1",
        "status": "passed" if failure is None and len(rounds) == args.rounds else "failed",
        "classification": "static_probe_and_testroot_fault_injection",
        "round_count": len(rounds),
        "required_round_count": args.rounds,
        "scenario_count": scenario_count,
        "assertion_count": scenario_count * 8,
        "duration_seconds": round(time.time() - started, 3),
        "static_scenarios": static,
        "rounds": rounds,
        "failure": failure,
        "preserved_test_root": preserved_test_root,
        "game_launched": False,
        "real_klei_writes": False,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"Report: {REPORT}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
