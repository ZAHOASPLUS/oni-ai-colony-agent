#!/usr/bin/env python3
"""Manifest-bound, crash-convergent ONI runtime bootstrap transaction.

The command never builds the Probe and never launches ONI or the Bridge.  It
installs four already-built workspace artifacts and a fail-closed Bridge data
tree.  ``--what-if`` is strictly read-only.  ``--apply`` publishes an
immutable run-scoped intent/plan/journal before the first ONI-root mutation.

An interrupted apply is resumed only when the exact immutable invocation,
source identities, and transaction latch still match.  A completed apply is
reported as an already-installed no-op; its physical steps are never replayed.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_ROOT = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
import manage_live_validation_environment as environment_core
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPARSE_ATTRIBUTE = 0x400
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_TREE_FILES = 100_000
MAX_TREE_BYTES = 4 * 1024 * 1024 * 1024
EXPECTED_MOD_VERSION = "0.7.0"

ENVIRONMENT_SCHEMA = "oni.live-validation-environment.v1"
ENVIRONMENT_JOURNAL_SCHEMA = "oni.live-validation-restore-journal.v1"
BOOTSTRAP_SCHEMA = "oni.v27-runtime-bootstrap.v1"
OWNER_SCHEMA = "oni.live-runtime-bootstrap-owner.v1"
INTENT_SCHEMA = "oni.live-runtime-bootstrap-intent.v1"
PLAN_SCHEMA = "oni.live-runtime-bootstrap-plan.v1"
JOURNAL_SCHEMA = "oni.live-runtime-bootstrap-journal-entry.v1"
RESULT_SCHEMA = "oni.live-runtime-bootstrap-result.v1"
RESULT_CANDIDATE_SCHEMA = "oni.live-runtime-bootstrap-result-candidate.v1"
SHARED_LOCK_SCHEMA = "oni.live-runtime-bootstrap-environment-lock.v1"
SHARED_LOCK_ATTEMPT_SCHEMA = "oni.live-runtime-bootstrap-environment-lock-attempt.v1"
SHARED_LOCK_CLAIM_SCHEMA = "oni.live-runtime-bootstrap-environment-lock-claim.v1"
SHARED_LOCK_RELEASE_SCHEMA = "oni.live-runtime-bootstrap-environment-lock-release.v1"
SHARED_LOCK_ATTEMPT_ROOT_CLAIM_SCHEMA = (
    "oni.live-runtime-bootstrap-environment-lock-attempt-root-claim.v1"
)
INIT_INTENT_SCHEMA = "oni.live-runtime-bootstrap-init-intent.v1"
INSTALL_STAGE_SCHEMA = "oni.live-runtime-bootstrap-install-stage.v1"
INIT_CLAIM_SCHEMA = "oni.live-runtime-bootstrap-init-claim.v1"
INIT_OUTCOME_UNKNOWN_SCHEMA = (
    "oni.live-runtime-bootstrap-initialization-outcome-unknown.v1"
)
ABORT_OWNER_SCHEMA = "oni.live-runtime-bootstrap-abort-owner.v1"
ABORT_INTENT_SCHEMA = "oni.live-runtime-bootstrap-abort-intent.v1"
ABORT_PLAN_SCHEMA = "oni.live-runtime-bootstrap-abort-plan.v1"
ABORT_JOURNAL_SCHEMA = "oni.live-runtime-bootstrap-abort-journal-entry.v1"
ABORT_CANDIDATE_SCHEMA = "oni.live-runtime-bootstrap-abort-candidate.v1"
ABORT_RESULT_SCHEMA = "oni.live-runtime-bootstrap-abort-result.v1"
ABORT_STAGE_SCHEMA = "oni.live-runtime-bootstrap-abort-stage.v1"

PROBE_RELATIVE = pathlib.PurePosixPath("mods/Dev/oni-ai-colony-agent")
BRIDGE_RELATIVE = pathlib.PurePosixPath("ai-bridge")
ARCHIVE_ROOT_RELATIVE = pathlib.PurePosixPath("ai-bridge-archives")
TRANSACTION_RELATIVE = pathlib.PurePosixPath("runtime-bootstrap")
ARTIFACT_NAMES = (
    "OniAiProbeMod.dll",
    "OniAiProbeMod.pdb",
    "mod.yaml",
    "mod_info.yaml",
)
COMMAND_QUEUES = (
    "pending",
    "approving",
    "approved",
    "executing",
    "confirming",
    "done",
    "failed",
    "rejected",
    "quarantine",
)
CONTROL_QUEUES = ("approved", "inflight", "done", "failed", "quarantine")
CONFIG_RELATIVES = (
    "kplayerprefs.yaml",
    "mods/mods.json",
    "mods/mods.json.bak",
    "mods/config.json",
)
JOURNAL_EVENT_ORDER = {
    "transaction_initialized": 0,
    "probe_staged": 10,
    "bridge_staged": 20,
    "archive_root_prepared": 25,
    "archive_root_ready": 30,
    "old_bridge_archived": 40,
    "old_probe_recovered": 50,
    "new_probe_installed": 60,
    "new_bridge_installed": 70,
    "commit_verified": 80,
}


class BootstrapError(RuntimeError):
    """A fail-closed infrastructure refusal."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


class CooperativeLockBusyError(BootstrapError):
    """A foreign shared-lock name blocked us before our inode was published."""


class InjectedCrash(RuntimeError):
    """TestRoot-only abrupt-stop simulation."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def document_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def exact_json_equal(left: Any, right: Any) -> bool:
    """JSON-contract equality that never treats booleans as integers."""

    try:
        return canonical_bytes(left) == canonical_bytes(right)
    except (TypeError, ValueError, OverflowError):
        return False


def normalized(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.normpath(str(pathlib.Path(path).expanduser()))))


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


def _logical_tree_paths(root: pathlib.Path | str) -> list[pathlib.Path]:
    """Enumerate through an OS path, but return only logical workflow paths."""

    logical_root = normalized(root)
    try:
        root_metadata = _lstat(logical_root)
    except OSError as exc:
        raise BootstrapError("tree root could not be inspected for enumeration") from exc
    if is_reparse_metadata(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise BootstrapError("tree root must be a non-reparse directory")
    pending = [(logical_root, node_identity(root_metadata))]
    seen_directory_identities = {tuple(node_identity(root_metadata))}
    logical_paths: list[pathlib.Path] = []
    while pending:
        directory, expected_identity = pending.pop()
        try:
            directory_metadata = _lstat(directory)
        except OSError as exc:
            raise BootstrapError("tree directory could not be inspected before enumeration") from exc
        if (
            is_reparse_metadata(directory_metadata)
            or not stat.S_ISDIR(directory_metadata.st_mode)
            or node_identity(directory_metadata) != expected_identity
        ):
            raise BootstrapError("tree directory changed before enumeration")
        directories: list[tuple[pathlib.Path, list[int]]] = []
        try:
            with os.scandir(_io_path(directory)) as iterator:
                for entry in iterator:
                    logical_path = directory / entry.name
                    try:
                        metadata = _lstat(logical_path)
                    except OSError as exc:
                        raise BootstrapError("tree entry could not be inspected during enumeration") from exc
                    if is_reparse_metadata(metadata):
                        raise BootstrapError("reparse points are forbidden in bootstrap trees")
                    logical_paths.append(logical_path)
                    if len(logical_paths) > MAX_TREE_FILES:
                        raise BootstrapError("bootstrap tree exceeds the safety bound")
                    if stat.S_ISDIR(metadata.st_mode):
                        identity = node_identity(metadata)
                        identity_key = tuple(identity)
                        if identity_key in seen_directory_identities:
                            raise BootstrapError("bootstrap tree contains a repeated directory identity")
                        seen_directory_identities.add(identity_key)
                        directories.append((logical_path, identity))
        except BootstrapError:
            raise
        except OSError as exc:
            raise BootstrapError("tree directory could not be enumerated") from exc
        try:
            directory_after = _lstat(directory)
        except OSError as exc:
            raise BootstrapError("tree directory could not be inspected after enumeration") from exc
        if (
            is_reparse_metadata(directory_after)
            or not stat.S_ISDIR(directory_after.st_mode)
            or node_identity(directory_after) != expected_identity
        ):
            raise BootstrapError("tree directory changed during enumeration")
        pending.extend(directories)
    try:
        root_after = _lstat(logical_root)
    except OSError as exc:
        raise BootstrapError("tree root could not be inspected after enumeration") from exc
    if (
        is_reparse_metadata(root_after)
        or not stat.S_ISDIR(root_after.st_mode)
        or node_identity(root_after) != node_identity(root_metadata)
    ):
        raise BootstrapError("tree root changed during enumeration")
    return logical_paths


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


def same_volume(*paths: pathlib.Path) -> bool:
    drives = {os.path.splitdrive(str(normalized(path)))[0].casefold() for path in paths}
    return len(drives) == 1


def path_id(path: pathlib.Path | str, *, upper: bool = False) -> str:
    text = str(normalized(path)).rstrip("\\/")
    text = text.upper() if upper else text.lower()
    return sha256_bytes(text.encode("utf-8"))


def text_id(value: str) -> str:
    return sha256_bytes(value.lower().encode("utf-8"))


def compact_file_state(path: pathlib.Path) -> dict[str, Any]:
    state = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
    return {
        "length": state["length"],
        "last_write_ns": state["last_write_ns"],
        "sha256": state["sha256"],
    }


def optional_compact_file_state(path: pathlib.Path) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {"exists": False}
    return {"exists": True, "state": compact_file_state(path)}


def capture_directory_custody(path: pathlib.Path, boundary: pathlib.Path) -> dict[str, Any]:
    """Capture an existing directory identity/mtime with a name sandwich."""

    path = normalized(path)
    assert_no_reparse(path, normalized(boundary))
    before = os.lstat(path)
    if not stat.S_ISDIR(before.st_mode) or is_reparse_metadata(before):
        raise BootstrapError("directory custody target is unsafe")
    state = {
        "identity": node_identity(before),
        "last_write_ns": int(before.st_mtime_ns),
    }
    after = os.lstat(path)
    if (
        node_identity(after) != state["identity"]
        or int(after.st_mtime_ns) != state["last_write_ns"]
        or is_reparse_metadata(after)
    ):
        raise BootstrapError("directory custody changed during capture")
    return state


def set_directory_mtime_by_owned_handle(
    path: pathlib.Path,
    expected_identity: Sequence[int],
    value_ns: int,
    *,
    after_open: Optional[Callable[[], None]] = None,
    before_set: Optional[Callable[[], None]] = None,
    after_set: Optional[Callable[[], None]] = None,
) -> None:
    """Set one Windows directory mtime without renaming it or its ancestors."""

    if os.name != "nt":
        raise BootstrapError("owned directory-mtime handles require Windows")
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt

    path = normalized(path)
    before = os.lstat(path)
    parent_before = os.lstat(path.parent)
    expected = list(expected_identity)
    if (
        is_reparse_metadata(before)
        or not stat.S_ISDIR(before.st_mode)
        or int(before.st_nlink) != 1
        or node_identity(before) != expected
        or is_reparse_metadata(parent_before)
        or not stat.S_ISDIR(parent_before.st_mode)
    ):
        raise BootstrapError("directory-mtime handle target custody is invalid")
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
    kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.SetFileTime.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    raw_handle = kernel32.CreateFileW(
        str(path),
        0x0080 | 0x0100,  # FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES
        0x0001 | 0x0002 | 0x0004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle in (None, invalid_handle):
        raise BootstrapError("directory-mtime handle could not be opened")
    descriptor: Optional[int] = None
    try:
        try:
            descriptor = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY)
        except OSError as exc:
            raise BootstrapError("directory-mtime handle could not be descriptor-bound") from exc
        raw_handle = None  # descriptor now owns the Windows handle
        opened = os.fstat(descriptor)
        opened_parent = os.lstat(path.parent)
        named = os.lstat(path)
        if (
            is_reparse_metadata(opened)
            or not stat.S_ISDIR(opened.st_mode)
            or int(opened.st_nlink) != 1
            or node_identity(opened) != expected
            or node_identity(named) != expected
            or is_reparse_metadata(named)
            or node_identity(opened_parent) != node_identity(parent_before)
            or int(opened_parent.st_mtime_ns) != int(parent_before.st_mtime_ns)
        ):
            raise BootstrapError("directory-mtime path/handle identity sandwich failed")
        if after_open is not None:
            after_open()
        # Revalidate the name after an adversarial hook.  If it was replaced,
        # the replacement handle is never touched.
        named_before_set = os.lstat(path)
        if (
            node_identity(named_before_set) != expected
            or is_reparse_metadata(named_before_set)
            or node_identity(os.fstat(descriptor)) != expected
        ):
            raise BootstrapError("directory-mtime pathname changed before SetFileTime")
        if before_set is not None:
            before_set()
        ticks = int(value_ns) // 100 + 116_444_736_000_000_000
        last_write = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        windows_handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        if not kernel32.SetFileTime(
            windows_handle, None, None, ctypes.byref(last_write)
        ):
            raise BootstrapError("directory-mtime SetFileTime failed")
        if after_set is not None:
            after_set()
        after_handle = os.fstat(descriptor)
        after_name = os.lstat(path)
        after_parent = os.lstat(path.parent)
        if (
            node_identity(after_handle) != expected
            or node_identity(after_name) != expected
            or is_reparse_metadata(after_name)
            or int(after_handle.st_mtime_ns) != int(value_ns)
            or int(after_name.st_mtime_ns) != int(value_ns)
            or node_identity(after_parent) != node_identity(parent_before)
            or int(after_parent.st_mtime_ns) != int(parent_before.st_mtime_ns)
        ):
            raise BootstrapError("directory-mtime post-SetFileTime custody is outcome_unknown")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif raw_handle not in (None, invalid_handle):
            kernel32.CloseHandle(raw_handle)


def is_reparse_metadata(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE
    )


def is_reparse(path: pathlib.Path) -> bool:
    return is_reparse_metadata(_lstat(path))


def node_identity(metadata: os.stat_result) -> list[int]:
    return [int(metadata.st_dev), int(metadata.st_ino), int(stat.S_IFMT(metadata.st_mode))]


def native_os_error_diagnostic(error: OSError) -> dict[str, Any]:
    """Return numeric native error details without disclosing path text."""

    errno_value = getattr(error, "errno", None)
    winerror_value = getattr(error, "winerror", None)
    return {
        "exception_type": type(error).__name__,
        "errno": errno_value if type(errno_value) is int else None,
        "winerror": winerror_value if type(winerror_value) is int else None,
        "filename_present": getattr(error, "filename", None) is not None,
        "filename2_present": getattr(error, "filename2", None) is not None,
        "filename_fields_redacted": True,
    }


def rename_endpoint_diagnostic(path: pathlib.Path) -> dict[str, Any]:
    """Observe one rename endpoint without returning its lexical path."""

    result: dict[str, Any] = {"path_id_lower": path_id(path)}
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        result["exists"] = False
    except OSError as exc:
        result["exists"] = None
        result["observation_error"] = native_os_error_diagnostic(exc)
    else:
        result.update(
            {
                "exists": True,
                "identity": node_identity(metadata),
                "kind": (
                    "directory"
                    if stat.S_ISDIR(metadata.st_mode)
                    else "file"
                    if stat.S_ISREG(metadata.st_mode)
                    else "other"
                ),
            }
        )
    return result


def rename_failure_diagnostic(
    error: OSError,
    source: pathlib.Path,
    target: pathlib.Path,
    label: str,
) -> dict[str, Any]:
    """Capture a redacted post-failure rename observation for later triage."""

    source_state = rename_endpoint_diagnostic(source)
    target_state = rename_endpoint_diagnostic(target)
    return {
        "schema": "oni.local-os-error-diagnostic.v1",
        "operation": "rename",
        "operation_label": label,
        "action_outcome": "unknown",
        "native_error": native_os_error_diagnostic(error),
        "source": source_state,
        "target": target_state,
    }


def assert_safe_root(path: pathlib.Path, label: str) -> pathlib.Path:
    root = normalized(path)
    if root.parent == root or str(root).startswith(("\\\\", "//")):
        raise BootstrapError(f"{label} cannot be a filesystem/UNC root")
    if not root.exists() or not root.is_dir():
        raise BootstrapError(f"{label} must be an existing directory")
    current = pathlib.Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        if os.path.lexists(current) and is_reparse(current):
            raise BootstrapError(f"{label} ancestry contains a reparse point")
    return root


def assert_no_reparse(
    path: pathlib.Path,
    boundary: pathlib.Path,
    *,
    allow_missing_leaf: bool = False,
) -> None:
    path = normalized(path)
    boundary = normalized(boundary)
    if not path_within(path, boundary, strict=False):
        raise BootstrapError("workflow path escaped its declared boundary")
    if not boundary.exists() or not boundary.is_dir() or is_reparse(boundary):
        raise BootstrapError("workflow boundary is missing or reparse-backed")
    relative = pathlib.Path(os.path.relpath(path, boundary))
    current = boundary
    for index, part in enumerate(relative.parts):
        if part == ".":
            continue
        current = current / part
        if os.path.lexists(current):
            if is_reparse(current):
                raise BootstrapError("reparse points are forbidden in bootstrap paths")
        elif not (allow_missing_leaf and index == len(relative.parts) - 1):
            raise BootstrapError("required bootstrap path is missing")


def stable_file(path: pathlib.Path, *, max_bytes: int, collect: bool = True) -> dict[str, Any]:
    path = normalized(path)
    io_path = _io_path(path)
    try:
        before = os.lstat(io_path)
    except OSError as exc:
        raise BootstrapError("required stable file is missing") from exc
    if is_reparse_metadata(before) or not stat.S_ISREG(before.st_mode) or int(before.st_nlink) != 1:
        raise BootstrapError("stable files must be non-reparse, single-link regular files")
    if int(before.st_size) > max_bytes:
        raise BootstrapError("stable file exceeds the size bound")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(io_path, flags)
    except OSError as exc:
        raise BootstrapError("stable file could not be descriptor-opened") from exc
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    length = 0
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (
            is_reparse_metadata(opened)
            or not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
            or node_identity(opened) != node_identity(before)
        ):
            raise BootstrapError("stable file identity changed before read")
        while True:
            chunk = handle.read(min(1024 * 1024, max_bytes - length + 1))
            if not chunk:
                break
            length += len(chunk)
            if length > max_bytes:
                raise BootstrapError("stable file exceeds the size bound")
            digest.update(chunk)
            if collect:
                chunks.append(chunk)
        after_handle = os.fstat(handle.fileno())
    try:
        after_path = os.lstat(io_path)
    except OSError as exc:
        raise BootstrapError("stable file disappeared after read") from exc
    stable_fields = lambda item: (
        node_identity(item),
        int(item.st_size),
        int(item.st_mtime_ns),
        int(item.st_nlink),
    )
    if (
        stable_fields(opened) != stable_fields(before)
        or stable_fields(after_handle) != stable_fields(opened)
        or stable_fields(after_path) != stable_fields(before)
        or int(after_handle.st_ctime_ns) != int(opened.st_ctime_ns)
        or int(after_path.st_ctime_ns) != int(before.st_ctime_ns)
        or length != int(opened.st_size)
    ):
        raise BootstrapError("stable file changed during descriptor read")
    return {
        "identity": node_identity(opened),
        "length": length,
        "last_write_ns": int(opened.st_mtime_ns),
        "sha256": "sha256:" + digest.hexdigest(),
        "bytes": b"".join(chunks) if collect else None,
    }


def parse_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BootstrapError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(
                BootstrapError(f"{label} contains non-finite JSON")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"{label} JSON root is not an object")
    return value


def stable_json(path: pathlib.Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = stable_file(path, max_bytes=MAX_JSON_BYTES, collect=True)
    raw = snapshot["bytes"]
    if not isinstance(raw, bytes) or raw.startswith(b"\xef\xbb\xbf"):
        raise BootstrapError(f"{label} must be BOM-free UTF-8 JSON")
    return parse_json_bytes(raw, label), snapshot


def file_record(path: pathlib.Path) -> dict[str, Any]:
    snapshot = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
    snapshot.pop("bytes", None)
    return snapshot


def tree_file_record(path: pathlib.Path, *, max_bytes: int = MAX_TREE_BYTES) -> dict[str, Any]:
    """Hash one tree member under the aggregate tree-size contract."""

    snapshot = stable_file(path, max_bytes=max_bytes, collect=False)
    snapshot.pop("bytes", None)
    return snapshot


def snapshot_tree(root: pathlib.Path) -> dict[str, Any]:
    root = normalized(root)
    if not os.path.lexists(_io_path(root)):
        return {"exists": False, "entries": []}
    metadata = _lstat(root)
    if is_reparse_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise BootstrapError("tree root must be a non-reparse directory")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    logical_paths = _logical_tree_paths(root)
    for path in sorted(logical_paths, key=lambda item: str(item).casefold()):
        item_metadata = _lstat(path)
        if is_reparse_metadata(item_metadata):
            raise BootstrapError("reparse points are forbidden in bootstrap trees")
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(item_metadata.st_mode):
            entries.append(
                {
                    "kind": "directory",
                    "relative": relative,
                    "last_write_ns": int(item_metadata.st_mtime_ns),
                }
            )
        elif stat.S_ISREG(item_metadata.st_mode):
            record = tree_file_record(path, max_bytes=MAX_TREE_BYTES - total_bytes)
            total_bytes += int(record["length"])
            entries.append(
                {
                    "kind": "file",
                    "relative": relative,
                    "length": record["length"],
                    "last_write_ns": record["last_write_ns"],
                    "sha256": record["sha256"],
                }
            )
        else:
            raise BootstrapError("bootstrap trees may contain only files/directories")
        if len(entries) > MAX_TREE_FILES or total_bytes > MAX_TREE_BYTES:
            raise BootstrapError("bootstrap tree exceeds the safety bound")
    return {
        "exists": True,
        "root_last_write_ns": int(metadata.st_mtime_ns),
        "entries": entries,
        "file_count": sum(item["kind"] == "file" for item in entries),
        "total_bytes": total_bytes,
    }


def identity_tree(root: pathlib.Path) -> dict[str, Any]:
    root = normalized(root)
    if not os.path.lexists(_io_path(root)):
        return {"exists": False, "entries": []}
    metadata = _lstat(root)
    if is_reparse_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise BootstrapError("identity tree root is unsafe")
    entries: list[dict[str, Any]] = []
    logical_paths = _logical_tree_paths(root)
    for path in sorted(logical_paths, key=lambda item: str(item).casefold()):
        item = _lstat(path)
        if is_reparse_metadata(item):
            raise BootstrapError("identity tree contains a reparse point")
        if stat.S_ISREG(item.st_mode) and int(item.st_nlink) != 1:
            raise BootstrapError("identity tree contains a hard-linked file")
        if not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode)):
            raise BootstrapError("identity tree contains a non-file node")
        entries.append(
            {
                "relative": path.relative_to(root).as_posix(),
                "kind": "file" if stat.S_ISREG(item.st_mode) else "directory",
                "identity": node_identity(item),
            }
        )
        if len(entries) > MAX_TREE_FILES:
            raise BootstrapError("identity tree exceeds the safety bound")
    return {
        "exists": True,
        "root_identity": node_identity(metadata),
        "entries": entries,
    }


def capture_bound_tree(root: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture content/timestamps and node identities in one stable sandwich."""

    root = normalized(root)
    if not os.path.lexists(_io_path(root)):
        return {"exists": False, "entries": []}, {"exists": False, "entries": []}
    root_before = _lstat(root)
    if is_reparse_metadata(root_before) or not stat.S_ISDIR(root_before.st_mode):
        raise BootstrapError("bound tree root is unsafe")
    paths = sorted(_logical_tree_paths(root), key=lambda item: str(item).casefold())
    snapshot_entries: list[dict[str, Any]] = []
    identity_entries: list[dict[str, Any]] = []
    stable_records: dict[str, tuple[str, dict[str, Any]]] = {}
    total_bytes = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        metadata = _lstat(path)
        if is_reparse_metadata(metadata):
            raise BootstrapError("bound tree contains a reparse point")
        if stat.S_ISDIR(metadata.st_mode):
            snapshot_entries.append(
                {
                    "kind": "directory",
                    "relative": relative,
                    "last_write_ns": int(metadata.st_mtime_ns),
                }
            )
            identity_entries.append(
                {"relative": relative, "kind": "directory", "identity": node_identity(metadata)}
            )
            stable_records[relative] = (
                "directory",
                {
                    "identity": node_identity(metadata),
                    "last_write_ns": int(metadata.st_mtime_ns),
                },
            )
        elif stat.S_ISREG(metadata.st_mode):
            record = tree_file_record(path, max_bytes=MAX_TREE_BYTES - total_bytes)
            total_bytes += int(record["length"])
            snapshot_entries.append(
                {
                    "kind": "file",
                    "relative": relative,
                    "length": record["length"],
                    "last_write_ns": record["last_write_ns"],
                    "sha256": record["sha256"],
                }
            )
            identity_entries.append(
                {"relative": relative, "kind": "file", "identity": record["identity"]}
            )
            stable_records[relative] = ("file", record)
        else:
            raise BootstrapError("bound tree contains a non-file node")
        if len(stable_records) > MAX_TREE_FILES or total_bytes > MAX_TREE_BYTES:
            raise BootstrapError("bound tree exceeds the safety bound")
    paths_after = sorted(_logical_tree_paths(root), key=lambda item: str(item).casefold())
    if [item.relative_to(root).as_posix() for item in paths_after] != list(stable_records):
        raise BootstrapError("bound tree pathset changed during capture")
    for path in paths_after:
        relative = path.relative_to(root).as_posix()
        kind, before = stable_records[relative]
        if kind == "directory":
            after = _lstat(path)
            if (
                is_reparse_metadata(after)
                or not stat.S_ISDIR(after.st_mode)
                or node_identity(after) != before["identity"]
                or int(after.st_mtime_ns) != before["last_write_ns"]
            ):
                raise BootstrapError("bound tree directory changed during capture")
        else:
            after = tree_file_record(path, max_bytes=int(before["length"]))
            if after != before:
                raise BootstrapError("bound tree file changed during capture")
    root_after = _lstat(root)
    if (
        is_reparse_metadata(root_after)
        or not stat.S_ISDIR(root_after.st_mode)
        or node_identity(root_after) != node_identity(root_before)
        or int(root_after.st_mtime_ns) != int(root_before.st_mtime_ns)
    ):
        raise BootstrapError("bound tree root changed during capture")
    snapshot = {
        "exists": True,
        "root_last_write_ns": int(root_before.st_mtime_ns),
        "entries": snapshot_entries,
        "file_count": sum(item["kind"] == "file" for item in snapshot_entries),
        "total_bytes": total_bytes,
    }
    identities = {
        "exists": True,
        "root_identity": node_identity(root_before),
        "entries": identity_entries,
    }
    return snapshot, identities


def tree_content(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if snapshot.get("exists") is not True:
        return {"exists": False, "entries": []}
    entries: list[dict[str, Any]] = []
    for item in snapshot.get("entries", []):
        if not isinstance(item, Mapping):
            raise BootstrapError("tree snapshot entry is invalid")
        if item.get("kind") == "directory":
            entries.append({"kind": "directory", "relative": item.get("relative")})
        elif item.get("kind") == "file":
            entries.append(
                {
                    "kind": "file",
                    "relative": item.get("relative"),
                    "length": item.get("length"),
                    "sha256": item.get("sha256"),
                }
            )
        else:
            raise BootstrapError("tree snapshot kind is invalid")
    return {"exists": True, "entries": entries}


def tree_matches_snapshot(root: pathlib.Path, expected: Mapping[str, Any]) -> bool:
    try:
        actual = snapshot_tree(root)
    except BootstrapError:
        return False
    if expected.get("exists") is not True:
        return actual.get("exists") is False
    comparable = {
        key: expected.get(key)
        for key in ("exists", "root_last_write_ns", "entries", "file_count", "total_bytes")
    }
    return actual == comparable


def tree_matches_content(root: pathlib.Path, expected: Mapping[str, Any]) -> bool:
    try:
        return exact_json_equal(tree_content(snapshot_tree(root)), dict(expected))
    except BootstrapError:
        return False


def tree_matches_identity(root: pathlib.Path, expected: Mapping[str, Any]) -> bool:
    try:
        return exact_json_equal(identity_tree(root), dict(expected))
    except BootstrapError:
        return False


def tree_matches_bound_content_identity(
    root: pathlib.Path,
    content: Mapping[str, Any],
    identities: Mapping[str, Any],
) -> bool:
    try:
        snapshot, actual_identities = capture_bound_tree(root)
        return exact_json_equal(tree_content(snapshot), dict(content)) and exact_json_equal(
            actual_identities, dict(identities)
        )
    except BootstrapError:
        return False


def optional_tree_state(
    root: pathlib.Path,
    snapshot: Mapping[str, Any],
    identities: Mapping[str, Any],
) -> bool:
    if snapshot.get("exists") is not True:
        return not os.path.lexists(root)
    try:
        actual_snapshot, actual_identity = capture_bound_tree(root)
    except BootstrapError:
        return False
    comparable = {
        key: snapshot.get(key)
        for key in ("exists", "root_last_write_ns", "entries", "file_count", "total_bytes")
    }
    return exact_json_equal(actual_snapshot, comparable) and exact_json_equal(
        actual_identity, dict(identities)
    )


def write_bytes_create_only(path: pathlib.Path, payload: bytes, label: str) -> None:
    path = normalized(path)
    if os.path.lexists(path):
        existing = stable_file(path, max_bytes=max(MAX_JSON_BYTES, len(payload)), collect=True)
        if existing.get("bytes") != payload:
            raise BootstrapError(f"{label} conflicts with an immutable publication")
        return
    temporary = path.with_name(path.name + ".new")
    temporary_identity: Optional[list[int]] = None
    if os.path.lexists(temporary):
        existing = stable_file(temporary, max_bytes=max(MAX_JSON_BYTES, len(payload)), collect=True)
        existing_bytes = existing.get("bytes")
        if (
            not isinstance(existing_bytes, bytes)
            or len(existing_bytes) > len(payload)
            or payload[: len(existing_bytes)] != existing_bytes
        ):
            raise BootstrapError(f"{label} staging publication was externally replaced")
        temporary_identity = list(existing["identity"])
        with temporary.open("r+b") as handle:
            opened = os.fstat(handle.fileno())
            if (
                node_identity(opened) != temporary_identity
                or int(opened.st_nlink) != 1
                or node_identity(os.lstat(temporary)) != temporary_identity
            ):
                raise BootstrapError(f"{label} partial staging identity changed")
            handle.seek(len(existing_bytes))
            handle.write(payload[len(existing_bytes) :])
            handle.flush()
            os.fsync(handle.fileno())
            if node_identity(os.fstat(handle.fileno())) != temporary_identity:
                raise BootstrapError(f"{label} partial staging was name-swapped")
    else:
        try:
            with temporary.open("xb") as handle:
                temporary_identity = node_identity(os.fstat(handle.fileno()))
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_identity = node_identity(os.fstat(handle.fileno()))
            if node_identity(os.lstat(temporary)) != temporary_identity:
                raise BootstrapError(f"{label} staging identity changed")
        except FileExistsError as exc:
            raise BootstrapError(f"{label} staging path appeared concurrently") from exc
    if (
        temporary_identity is None
        or node_identity(os.lstat(temporary)) != temporary_identity
        or int(os.lstat(temporary).st_nlink) != 1
        or os.path.lexists(path)
    ):
        raise BootstrapError(f"{label} staging ownership changed before publication")
    try:
        os.rename(temporary, path)
    except OSError as exc:
        raise BootstrapError(f"{label} could not be published create-only") from exc
    published = stable_file(path, max_bytes=max(MAX_JSON_BYTES, len(payload)), collect=True)
    if published.get("bytes") != payload or published.get("identity") != temporary_identity:
        raise BootstrapError(f"{label} publication verification failed")


def write_json_create_only(path: pathlib.Path, value: Mapping[str, Any], label: str) -> None:
    write_bytes_create_only(path, document_bytes(value), label)


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise BootstrapError(f"{label} must be a canonical sha256 value")
    return value


def require_exact_type_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise BootstrapError(f"{label} must be an exact integer")
    return value


@dataclass(frozen=True)
class Roots:
    run_id: str
    oni_root: pathlib.Path
    evidence_root: pathlib.Path
    test_root: Optional[pathlib.Path]

    @property
    def environment_dir(self) -> pathlib.Path:
        return self.evidence_root / self.run_id / "environment"

    @property
    def transaction_dir(self) -> pathlib.Path:
        return self.evidence_root / self.run_id / pathlib.Path(*TRANSACTION_RELATIVE.parts)


def resolve_roots(args: argparse.Namespace) -> Roots:
    if RUN_ID_RE.fullmatch(str(args.run_id or "")) is None:
        raise BootstrapError("run id is invalid")
    if args.confirm_live_run_id != args.run_id:
        raise BootstrapError("an exact --confirm-live-run-id is required")
    test_root = assert_safe_root(pathlib.Path(args.test_root), "TestRoot") if args.test_root else None
    oni_root = assert_safe_root(pathlib.Path(args.oni_root), "ONI root")
    evidence_root = assert_safe_root(pathlib.Path(args.evidence_root), "evidence root")
    live_default = normalized(
        pathlib.Path(os.environ.get("USERPROFILE", str(pathlib.Path.home())))
        / "Documents"
        / "Klei"
        / "OxygenNotIncluded"
    )
    if test_root is not None:
        if not path_within(oni_root, test_root) or not path_within(evidence_root, test_root):
            raise BootstrapError("fixture roots must be strict TestRoot descendants")
        if paths_overlap(test_root, live_default) or paths_overlap(test_root, PROJECT_ROOT):
            raise BootstrapError("TestRoot overlaps live ONI data or the workspace")
    else:
        if not same_path(oni_root, live_default):
            raise BootstrapError("live mode is restricted to the canonical ONI user root")
        if not path_within(evidence_root, PROJECT_ROOT):
            raise BootstrapError("live bootstrap evidence must stay inside the workspace")
    if paths_overlap(oni_root, evidence_root) or not same_volume(oni_root, evidence_root):
        raise BootstrapError("ONI and evidence roots must be disjoint on one local volume")
    assert_no_reparse(oni_root, test_root or oni_root)
    assert_no_reparse(evidence_root, test_root or PROJECT_ROOT)
    return Roots(args.run_id, oni_root, evidence_root, test_root)


def _process_basename(value: Any) -> str:
    """Return a case-folded Windows-compatible executable/script basename."""

    text = str(value or "").strip().replace("/", "\\")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    return text.rsplit("\\", 1)[-1].casefold()


def _is_oni_bridge_script_argv(argv: Sequence[str]) -> bool:
    """Match the Python script argv, never a ``-c``/``-m`` data argument."""

    if not argv:
        return False
    index = 1
    while index < len(argv):
        argument = str(argv[index])
        folded = argument.casefold()
        if folded in {"-c", "-m"} or folded.startswith("-c") or folded.startswith("-m"):
            return False
        if folded in {"-w", "-x", "--check-hash-based-pycs"}:
            index += 2
            continue
        if folded == "--":
            index += 1
            break
        if folded.startswith("-"):
            index += 1
            continue
        break
    return index < len(argv) and _process_basename(argv[index]) == "oni_bridge.py"


def read_process_counts(args: argparse.Namespace, roots: Roots) -> dict[str, int]:
    if roots.test_root is not None:
        if not args.process_state_fixture:
            raise BootstrapError("TestRoot requires --process-state-fixture")
        fixture = normalized(args.process_state_fixture)
        if not path_within(fixture, roots.test_root):
            raise BootstrapError("process fixture escaped TestRoot")
        value, _ = stable_json(fixture, "process-state fixture")
        oni = require_exact_type_int(value.get("OxygenNotIncluded"), "ONI process count")
        bridge = require_exact_type_int(value.get("oni_bridge.py"), "Bridge process count")
        if oni < 0 or bridge < 0:
            raise BootstrapError("process counts cannot be negative")
        return {"OxygenNotIncluded": oni, "oni_bridge.py": bridge}
    command = (
        "$ErrorActionPreference='Stop';"
        "$gp=$false;$gc=$false;$o=0;$c=@();"
        "try{$all=@(Get-Process -ErrorAction Stop);$o=@($all|Where-Object {$_.ProcessName -ceq 'OxygenNotIncluded'}).Count;$gp=$true}catch{};"
        "try{$c=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='py.exe'\" |"
        "Select-Object ProcessId,Name,CommandLine);$gc=$true}catch{};"
        "@{get_process_succeeded=$gp;get_cim_succeeded=$gc;OxygenNotIncluded=$o;candidates=$c}|"
        "ConvertTo-Json -Compress -Depth 5"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(
            "live process census could not read Python candidate command lines",
            diagnostic={
                "schema": "oni.live-runtime-bootstrap-process-census-refusal.v1",
                "status": "blocked",
                "classification": "infra_fail",
                "action_outcome": "unknown",
                "reason": "process_census_read_failed",
                "error_type": type(exc).__name__,
                "native_os_error": (
                    native_os_error_diagnostic(exc) if isinstance(exc, OSError) else None
                ),
            },
        ) from exc
    if completed.returncode != 0:
        raise BootstrapError(
            "live process census failed",
            diagnostic={
                "schema": "oni.live-runtime-bootstrap-process-census-refusal.v1",
                "status": "blocked",
                "classification": "infra_fail",
                "action_outcome": "unknown",
                "reason": "process_census_nonzero_exit",
                "return_code": completed.returncode,
            },
        )
    try:
        value = json.loads(completed.stdout)
        if value.get("get_process_succeeded") is not True or value.get("get_cim_succeeded") is not True:
            raise BootstrapError(
                "live process census was incomplete",
                diagnostic={
                    "schema": "oni.live-runtime-bootstrap-process-census-refusal.v1",
                    "status": "blocked",
                    "classification": "infra_fail",
                    "action_outcome": "unknown",
                    "reason": "process_census_incomplete",
                },
            )
        oni = require_exact_type_int(value.get("OxygenNotIncluded"), "ONI process count")
        candidates = value.get("candidates")
        if candidates is None:
            candidates = []
        elif isinstance(candidates, Mapping):
            candidates = [candidates]
        if not isinstance(candidates, list):
            raise BootstrapError("live Bridge process candidates are invalid")
        bridge = 0
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping):
                raise BootstrapError("live Bridge process candidate is invalid")
            if set(candidate) != {"ProcessId", "Name", "CommandLine"}:
                raise BootstrapError("live Bridge process candidate shape is invalid")
            process_id = candidate.get("ProcessId")
            process_name = candidate.get("Name")
            if (
                type(process_id) is not int
                or process_id <= 0
                or not isinstance(process_name, str)
                or process_name.casefold() not in {"python.exe", "pythonw.exe", "py.exe"}
            ):
                raise BootstrapError("live Bridge process candidate identity is invalid")
            command_line = candidate.get("CommandLine")
            if not isinstance(command_line, str) or not command_line.strip():
                if isinstance(command_line, str):
                    reason = "command_line_empty"
                elif command_line is None:
                    reason = "command_line_unreadable"
                else:
                    reason = "command_line_invalid_type"
                raise BootstrapError(
                    "live process census could not read a Python candidate command line",
                    diagnostic={
                        "schema": "oni.live-runtime-bootstrap-process-census-refusal.v1",
                        "status": "blocked",
                        "classification": "infra_fail",
                        "action_outcome": "unknown",
                        "reason": reason,
                        "candidate_index": candidate_index,
                        "process_id": process_id,
                        "process_name": process_name,
                        "command_line_type": type(command_line).__name__,
                    },
                )
            try:
                argv = windows_command_line_argv(command_line)
            except (BootstrapError, OSError, ValueError, TypeError) as exc:
                raise BootstrapError(
                    "live process census could not parse a Python candidate command line",
                    diagnostic={
                        "schema": "oni.live-runtime-bootstrap-process-census-refusal.v1",
                        "status": "blocked",
                        "classification": "infra_fail",
                        "action_outcome": "unknown",
                        "reason": "command_line_parse_failed",
                        "candidate_index": candidate_index,
                        "process_id": process_id,
                        "process_name": process_name,
                        "native_os_error": (
                            native_os_error_diagnostic(exc) if isinstance(exc, OSError) else None
                        ),
                    },
                ) from exc
            if _is_oni_bridge_script_argv(argv):
                bridge += 1
    except (json.JSONDecodeError, AttributeError) as exc:
        raise BootstrapError("live process census was invalid") from exc
    return {"OxygenNotIncluded": oni, "oni_bridge.py": bridge}


def windows_command_line_argv(command_line: str) -> list[str]:
    if os.name != "nt":
        # Live mode is Windows-only; this branch keeps source-level tests
        # deterministic on other hosts.
        import shlex

        return shlex.split(command_line, posix=False)
    import ctypes

    argc = ctypes.c_int()
    parser = ctypes.windll.shell32.CommandLineToArgvW
    parser.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv_pointer = parser(command_line, ctypes.byref(argc))
    if not argv_pointer:
        raise BootstrapError("Windows process command line could not be parsed")
    try:
        return [argv_pointer[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv_pointer)


def require_processes_stopped(args: argparse.Namespace, roots: Roots) -> dict[str, int]:
    counts = read_process_counts(args, roots)
    if counts != {"OxygenNotIncluded": 0, "oni_bridge.py": 0}:
        raise BootstrapError("ONI and oni_bridge.py must both be stopped")
    return counts


def load_environment(
    args: argparse.Namespace,
    roots: Roots,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest_path = normalized(args.environment_manifest)
    expected_path = roots.environment_dir / "environment-manifest.json"
    if not same_path(manifest_path, expected_path):
        raise BootstrapError("environment manifest is not at the unique run path")
    assert_no_reparse(manifest_path, roots.evidence_root)
    environment_before = os.lstat(roots.environment_dir)
    if is_reparse_metadata(environment_before) or not stat.S_ISDIR(environment_before.st_mode):
        raise BootstrapError("environment evidence directory is unsafe")
    manifest, manifest_state = stable_json(manifest_path, "environment manifest")
    expected_hash = require_hash(
        args.expected_environment_manifest_sha256,
        "external environment manifest hash",
    )
    if manifest_state["sha256"] != expected_hash:
        raise BootstrapError("environment manifest does not match its external hash")
    expected_mode = "test_fixture" if roots.test_root is not None else "live"
    if (
        manifest.get("schema") != ENVIRONMENT_SCHEMA
        or manifest.get("run_id") != roots.run_id
        or manifest.get("mode") != expected_mode
    ):
        raise BootstrapError("environment manifest does not bind this run/mode")
    root_binding = manifest.get("roots") if isinstance(manifest.get("roots"), Mapping) else {}
    if (
        root_binding.get("oni_root_id_lower") != path_id(roots.oni_root)
        or root_binding.get("oni_root_id_upper") != path_id(roots.oni_root, upper=True)
        or root_binding.get("evidence_root_id_lower") != path_id(roots.evidence_root)
        or root_binding.get("evidence_root_id_upper") != path_id(roots.evidence_root, upper=True)
        or root_binding.get("same_local_volume") is not True
    ):
        raise BootstrapError("environment manifest root binding is invalid")
    backup = manifest.get("backup") if isinstance(manifest.get("backup"), Mapping) else {}
    for key, relative in (("probe", str(PROBE_RELATIVE)), ("old_bridge", str(BRIDGE_RELATIVE))):
        record = backup.get(key) if isinstance(backup.get(key), Mapping) else {}
        if (
            record.get("relative") != relative
            or record.get("path_id_lower") != path_id(roots.oni_root / pathlib.Path(relative))
            or record.get("path_id_upper")
            != path_id(roots.oni_root / pathlib.Path(relative), upper=True)
            or not isinstance(record.get("snapshot"), Mapping)
        ):
            raise BootstrapError(f"environment {key} backup binding is invalid")
    archive = backup.get("bridge_archive_root") if isinstance(backup.get("bridge_archive_root"), Mapping) else {}
    archive_path = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    if (
        archive.get("relative") != str(ARCHIVE_ROOT_RELATIVE)
        or archive.get("path_id_lower") != path_id(archive_path)
        or archive.get("path_id_upper") != path_id(archive_path, upper=True)
        or not isinstance(archive.get("snapshot"), Mapping)
    ):
        raise BootstrapError("environment archive-root binding is invalid")
    journal_path = roots.environment_dir / "restore-journal.jsonl"
    journal_state = stable_file(journal_path, max_bytes=MAX_JSON_BYTES, collect=True)
    raw = journal_state.get("bytes")
    if not isinstance(raw, bytes):
        raise BootstrapError("environment restore journal is unreadable")
    try:
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    except UnicodeError as exc:
        raise BootstrapError("environment restore journal is empty/invalid") from exc
    if not lines:
        raise BootstrapError("environment restore journal is empty/invalid")
    entries: list[dict[str, Any]] = []
    previous: Optional[str] = None
    required_keys = {
        "schema",
        "index",
        "recorded_at",
        "event",
        "data",
        "previous_entry_hash",
        "entry_hash",
    }
    for line in lines:
        entry = parse_json_bytes(line.encode("utf-8"), "environment restore journal")
        if (
            set(entry) != required_keys
            or entry.get("schema") != ENVIRONMENT_JOURNAL_SCHEMA
            or type(entry.get("index")) is not int
            or entry.get("index") != len(entries) + 1
            or entry.get("previous_entry_hash") != previous
            or not isinstance(entry.get("recorded_at"), str)
            or not isinstance(entry.get("event"), str)
            or not entry.get("event")
            or not isinstance(entry.get("data"), Mapping)
        ):
            raise BootstrapError("environment restore journal schema/order is invalid")
        try:
            recorded_at = dt.datetime.fromisoformat(
                entry["recorded_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise BootstrapError("environment restore journal timestamp is invalid") from exc
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise BootstrapError("environment restore journal timestamp is invalid")
        claimed = require_hash(entry.get("entry_hash"), "environment journal entry hash")
        unhashed = dict(entry)
        del unhashed["entry_hash"]
        if canonical_hash(unhashed) != claimed:
            raise BootstrapError("environment restore journal hash chain is invalid")
        entries.append(entry)
        previous = claimed
    first = entries[0]
    data = first.get("data") if isinstance(first.get("data"), Mapping) else {}
    if (
        first.get("event") != "environment_prepared"
        or first.get("index") != 1
        or first.get("previous_entry_hash") is not None
        or set(data)
        != {
            "environment_manifest_sha256",
            "pre_boundary_sha256",
            "clone_manifest_sha256",
        }
        or data.get("environment_manifest_sha256") != expected_hash
        or HASH_RE.fullmatch(str(data.get("pre_boundary_sha256") or "")) is None
        or HASH_RE.fullmatch(str(data.get("clone_manifest_sha256") or "")) is None
    ):
        raise BootstrapError("environment restore journal does not bind the manifest")
    if len(entries) != 1:
        raise BootstrapError("runtime bootstrap requires the unique pre-config environment journal origin")
    environment_binding = {
        "path_id_lower": path_id(manifest_path),
        "sha256": manifest_state["sha256"],
        "length": manifest_state["length"],
        "last_write_ns": manifest_state["last_write_ns"],
        "identity": manifest_state["identity"],
        "initial_restore_journal_entry_hash": first["entry_hash"],
        "restore_journal_identity": journal_state["identity"],
        "restore_journal_sha256": journal_state["sha256"],
        "restore_journal_length": journal_state["length"],
        "restore_journal_tip_hash": entries[-1]["entry_hash"],
        "restore_journal_entry_count": len(entries),
        "environment_directory_identity": node_identity(environment_before),
    }
    environment_after = os.lstat(roots.environment_dir)
    if node_identity(environment_after) != node_identity(environment_before):
        raise BootstrapError("environment evidence directory was name-swapped")
    return manifest, environment_binding, {"first": first, "path": journal_path}


def release_artifacts(args: argparse.Namespace, roots: Roots) -> tuple[dict[str, Any], dict[str, bytes]]:
    dll = normalized(args.release_dll)
    if dll.name != ARTIFACT_NAMES[0]:
        raise BootstrapError("release DLL must have the canonical Probe filename")
    release_dir = dll.parent
    if roots.test_root is not None:
        if not path_within(release_dir, roots.test_root):
            raise BootstrapError("fixture release artifacts escaped TestRoot")
    else:
        expected_dir = PROJECT_ROOT / "mod"
        if not same_path(release_dir, expected_dir):
            raise BootstrapError("live release artifacts must come from the workspace mod directory")
    assert_no_reparse(release_dir, roots.test_root or PROJECT_ROOT)
    directory_before = os.lstat(release_dir)
    if is_reparse_metadata(directory_before) or not stat.S_ISDIR(directory_before.st_mode):
        raise BootstrapError("release artifact directory is unsafe")
    release_parent_binding = {
        "identity": node_identity(directory_before),
        "last_write_ns": int(directory_before.st_mtime_ns),
    }
    expected_dll_hash = require_hash(args.expected_release_dll_sha256, "external release DLL hash")
    records: dict[str, Any] = {}
    payloads: dict[str, bytes] = {}
    for name in ARTIFACT_NAMES:
        path = release_dir / name
        snapshot = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=True)
        payload = snapshot.pop("bytes")
        if not isinstance(payload, bytes):
            raise BootstrapError("release artifact stable read returned no bytes")
        records[name] = {
            "path_id_lower": path_id(path),
            "path_id_upper": path_id(path, upper=True),
            "parent_identity": release_parent_binding["identity"],
            "parent_last_write_ns": release_parent_binding["last_write_ns"],
            **snapshot,
        }
        payloads[name] = payload
    directory_after = os.lstat(release_dir)
    if (
        node_identity(directory_after) != release_parent_binding["identity"]
        or int(directory_after.st_mtime_ns) != release_parent_binding["last_write_ns"]
    ):
        raise BootstrapError("release artifact directory changed during four-file capture")
    for name in ARTIFACT_NAMES:
        current = stable_file(release_dir / name, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
        current.pop("bytes", None)
        expected = {
            key: value
            for key, value in records[name].items()
            if key not in {"path_id_lower", "path_id_upper", "parent_identity", "parent_last_write_ns"}
        }
        if not exact_json_equal(current, expected):
            raise BootstrapError("release artifact pathset changed during binding")
    if records[ARTIFACT_NAMES[0]]["sha256"] != expected_dll_hash:
        raise BootstrapError("Release DLL does not match its external hash")
    return records, payloads


def validate_release_anchors(args: argparse.Namespace, roots: Roots, plan: Mapping[str, Any]) -> None:
    current, _ = release_artifacts(args, roots)
    expected = plan.get("inputs", {}).get("release_artifacts")
    if not exact_json_equal(current, expected):
        raise BootstrapError("workspace release artifacts changed after intent publication")


def physical_commit_started(roots: Roots, plan: Mapping[str, Any]) -> bool:
    transaction = roots.transaction_dir
    if not os.path.lexists(transaction / "journal"):
        return False
    for event_name in (
        "old_bridge_archived",
        "old_probe_recovered",
        "new_probe_installed",
        "new_bridge_installed",
        "commit_verified",
    ):
        if journal_event(transaction, event_name) is not None:
            return True
    ready = journal_event(transaction, "archive_root_ready")
    if ready is not None and ready.get("data", {}).get("created") is True:
        return True
    prepared = journal_event(transaction, "archive_root_prepared")
    if prepared is not None:
        archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
        prepared_identity = prepared.get("data", {}).get("identity")
        if isinstance(prepared_identity, Mapping) and tree_matches_identity(
            archive_root, prepared_identity
        ):
            return True
    old_bridge = plan.get("prestate", {}).get("old_bridge", {})
    archive_relative = plan.get("desired", {}).get("archive_relative")
    if old_bridge.get("snapshot", {}).get("exists") is True and archive_relative:
        archive = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(archive_relative).parts)
        if optional_tree_state(archive, old_bridge["snapshot"], old_bridge["identity"]):
            return True
    old_probe = plan.get("prestate", {}).get("probe", {})
    recovery = transaction / "recovery" / "old-probe"
    if old_probe.get("snapshot", {}).get("exists") is True and optional_tree_state(
        recovery, old_probe["snapshot"], old_probe["identity"]
    ):
        return True
    return False


def frozen_probe_payloads(roots: Roots, plan: Mapping[str, Any]) -> dict[str, bytes]:
    event = journal_event(roots.transaction_dir, "probe_staged")
    if event is None:
        raise BootstrapError("workspace drift occurred before Probe stage custody was frozen")
    expected_content = {
        "exists": True,
        "entries": [
            {
                "kind": "file",
                "relative": name,
                "length": plan["inputs"]["release_artifacts"][name]["length"],
                "sha256": plan["inputs"]["release_artifacts"][name]["sha256"],
            }
            for name in sorted(ARTIFACT_NAMES, key=str.casefold)
        ],
    }
    candidates = (
        roots.transaction_dir / "stage" / "probe",
        roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts),
    )
    source: Optional[pathlib.Path] = None
    for candidate in candidates:
        if tree_matches_bound_content_identity(candidate, expected_content, event["data"]["identity"]):
            source = candidate
            break
    if source is None:
        raise BootstrapError("frozen Probe stage/live custody is unavailable")
    payloads: dict[str, bytes] = {}
    for name in ARTIFACT_NAMES:
        state = stable_file(source / name, max_bytes=MAX_ARTIFACT_BYTES, collect=True)
        if (
            state["sha256"] != plan["inputs"]["release_artifacts"][name]["sha256"]
            or state["length"] != plan["inputs"]["release_artifacts"][name]["length"]
            or not isinstance(state.get("bytes"), bytes)
        ):
            raise BootstrapError("frozen Probe artifact does not match the plan")
        payloads[name] = state["bytes"]
    return payloads


def validate_manifest_anchor(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest, binding, journal = load_environment(args, roots)
    if not exact_json_equal(
        binding, plan.get("inputs", {}).get("environment_manifest")
    ):
        raise BootstrapError("environment manifest identity changed after intent publication")
    if journal["first"].get("entry_hash") != binding["initial_restore_journal_entry_hash"]:
        raise BootstrapError("environment restore-journal origin changed")
    return manifest, journal


def desired_documents(
    roots: Roots,
    manifest: Mapping[str, Any],
    dll_hash: str,
    transaction_id: str,
    old_bridge_exists: bool,
    expected_mod_version: str,
) -> dict[str, Any]:
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise BootstrapError("environment manifest created_at is invalid")
    bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    archive = archive_root / f"{roots.run_id}-preexisting-{transaction_id}"
    bootstrap = {
        "schema": BOOTSTRAP_SCHEMA,
        "run_id": roots.run_id,
        "prepared_at": created_at,
        "expected_mod_version": expected_mod_version,
        "bridge_dir": str(bridge),
        "archived_previous_bridge": str(archive) if old_bridge_exists else None,
        "built_dll_sha256": dll_hash,
        "installed_dll_sha256": dll_hash,
        "installed_release": True,
        "action_policy_disabled": True,
    }
    action_policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": False,
        "suggestions_enabled": False,
        "allowed_save_hashes": [],
        "allowed_actions": ["focus_note", "noop"],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
        "generated_at": created_at,
    }
    control_policy = {
        "schema": "oni.validation-control-policy.v1",
        "controls_enabled": False,
        "test_run_id": None,
        "allowed_operations": [],
        "allowed_save_hashes": [],
        "allowed_save_path_ids": [],
        "allowed_target_save_path_ids": [],
        "generated_at": created_at,
        "expires_at": None,
    }
    return {
        "bootstrap": bootstrap,
        "action_policy": action_policy,
        "validation_control_policy": control_policy,
        "archive_relative": (
            f"{ARCHIVE_ROOT_RELATIVE.as_posix()}/{archive.name}" if old_bridge_exists else None
        ),
    }


def capture_initial_plan(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    environment_binding: Mapping[str, Any],
    releases: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if args.expected_mod_version != EXPECTED_MOD_VERSION:
        raise BootstrapError(f"expected mod version must be exactly {EXPECTED_MOD_VERSION}")
    backup = manifest["backup"]
    probe = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
    bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    for path in (probe, bridge, archive_root):
        assert_no_reparse(path, roots.oni_root, allow_missing_leaf=True)
    probe_snapshot = dict(backup["probe"]["snapshot"])
    bridge_snapshot = dict(backup["old_bridge"]["snapshot"])
    archive_snapshot = dict(backup["bridge_archive_root"]["snapshot"])
    actual_probe_snapshot, probe_identity = capture_bound_tree(probe)
    actual_bridge_snapshot, bridge_identity = capture_bound_tree(bridge)
    actual_archive_snapshot, archive_identity = capture_bound_tree(archive_root)
    def expected_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
        if value.get("exists") is not True:
            return {"exists": False, "entries": []}
        return {
            key: value.get(key)
            for key in ("exists", "root_last_write_ns", "entries", "file_count", "total_bytes")
        }
    if not exact_json_equal(actual_probe_snapshot, expected_snapshot(probe_snapshot)):
        raise BootstrapError("current Probe tree does not match the environment manifest")
    if not exact_json_equal(actual_bridge_snapshot, expected_snapshot(bridge_snapshot)):
        raise BootstrapError("current Bridge tree does not match the environment manifest")
    if not exact_json_equal(actual_archive_snapshot, expected_snapshot(archive_snapshot)):
        raise BootstrapError("current archive tree does not match the environment manifest")
    prestate = {
        "probe": {"snapshot": probe_snapshot, "identity": probe_identity},
        "old_bridge": {"snapshot": bridge_snapshot, "identity": bridge_identity},
        "archive_root": {"snapshot": archive_snapshot, "identity": archive_identity},
        "parent_directories": {
            "oni_root": capture_directory_custody(roots.oni_root, roots.oni_root),
            "probe_container": capture_directory_custody(
                probe.parent.parent, roots.oni_root
            ),
            "probe_parent": capture_directory_custody(probe.parent, roots.oni_root),
        },
    }
    seed = {
        "schema": "oni.live-runtime-bootstrap-seed.v1",
        "run_id": roots.run_id,
        "expected_mod_version": args.expected_mod_version,
        "inputs": {
            "environment_manifest": dict(environment_binding),
            "release_artifacts": dict(releases),
        },
        "prestate": prestate,
        "targets": {
            "probe_relative": PROBE_RELATIVE.as_posix(),
            "bridge_relative": BRIDGE_RELATIVE.as_posix(),
            "archive_root_relative": ARCHIVE_ROOT_RELATIVE.as_posix(),
            "evidence_relative": f"{roots.run_id}/{TRANSACTION_RELATIVE.as_posix()}",
        },
    }
    seed_hash = canonical_hash(seed)
    transaction_id = "rtb-" + seed_hash.removeprefix("sha256:")[:24]
    desired = desired_documents(
        roots,
        manifest,
        releases[ARTIFACT_NAMES[0]]["sha256"],
        transaction_id,
        bridge_snapshot.get("exists") is True,
        args.expected_mod_version,
    )
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "transaction_id": transaction_id,
        "run_id": roots.run_id,
        "expected_mod_version": args.expected_mod_version,
        "seed_hash": seed_hash,
        "inputs": seed["inputs"],
        "prestate": prestate,
        "targets": seed["targets"],
        "desired": desired,
        "physical_steps": [
            "stage_probe",
            "stage_bridge",
            "ensure_archive_root_if_needed",
            "archive_old_bridge_if_present",
            "recover_old_probe_if_present",
            "install_probe",
            "install_bridge",
        ],
    }
    plan["plan_hash"] = canonical_hash(plan)
    intent: dict[str, Any] = {
        "schema": INTENT_SCHEMA,
        "transaction_id": transaction_id,
        "run_id": roots.run_id,
        "plan_hash": plan["plan_hash"],
        "environment_manifest_sha256": environment_binding["sha256"],
        "release_dll_sha256": releases[ARTIFACT_NAMES[0]]["sha256"],
        "expected_mod_version": args.expected_mod_version,
        "confirm_live_run_id": args.confirm_live_run_id,
        "operation": "install_prebuilt_release",
    }
    intent["intent_hash"] = canonical_hash(intent)
    return intent, plan


def validate_plan_shape(plan: Mapping[str, Any]) -> None:
    if (
        plan.get("schema") != PLAN_SCHEMA
        or not isinstance(plan.get("transaction_id"), str)
        or not isinstance(plan.get("run_id"), str)
        or require_hash(plan.get("seed_hash"), "plan seed hash") is None
        or require_hash(plan.get("plan_hash"), "plan hash") is None
    ):
        raise BootstrapError("runtime bootstrap plan shape is invalid")
    unhashed = dict(plan)
    claimed = unhashed.pop("plan_hash", None)
    if canonical_hash(unhashed) != claimed:
        raise BootstrapError("runtime bootstrap plan hash is invalid")


def validate_intent_shape(intent: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    if (
        intent.get("schema") != INTENT_SCHEMA
        or intent.get("transaction_id") != plan.get("transaction_id")
        or intent.get("run_id") != plan.get("run_id")
        or intent.get("plan_hash") != plan.get("plan_hash")
    ):
        raise BootstrapError("runtime bootstrap intent binding is invalid")
    unhashed = dict(intent)
    claimed = unhashed.pop("intent_hash", None)
    if canonical_hash(unhashed) != claimed:
        raise BootstrapError("runtime bootstrap intent hash is invalid")


def validate_identity_contract(
    value: Any,
    snapshot: Mapping[str, Any],
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise BootstrapError(f"{label} identity tree is invalid")
    if snapshot.get("exists") is not True:
        if not exact_json_equal(dict(value), {"exists": False, "entries": []}):
            raise BootstrapError(f"{label} absent identity contract is invalid")
        return
    if set(value) != {"exists", "root_identity", "entries"} or value.get("exists") is not True:
        raise BootstrapError(f"{label} identity contract shape is invalid")
    root_identity = value.get("root_identity")
    if (
        not isinstance(root_identity, list)
        or len(root_identity) != 3
        or any(type(item) is not int or item < 0 for item in root_identity)
    ):
        raise BootstrapError(f"{label} root identity is invalid")
    expected_shape = [
        (str(item.get("relative")), str(item.get("kind")))
        for item in snapshot.get("entries", [])
    ]
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) != len(expected_shape):
        raise BootstrapError(f"{label} identity entry count is invalid")
    actual_shape: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"relative", "kind", "identity"}:
            raise BootstrapError(f"{label} identity entry shape is invalid")
        identity_value = entry.get("identity")
        if (
            not isinstance(identity_value, list)
            or len(identity_value) != 3
            or any(type(item) is not int or item < 0 for item in identity_value)
        ):
            raise BootstrapError(f"{label} child identity is invalid")
        actual_shape.append((str(entry.get("relative")), str(entry.get("kind"))))
    if actual_shape != expected_shape:
        raise BootstrapError(f"{label} identity entries do not match the manifest snapshot")


def validate_deterministic_contract(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    environment_binding: Mapping[str, Any],
    releases: Mapping[str, Any],
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    plan_keys = {
        "schema",
        "transaction_id",
        "run_id",
        "expected_mod_version",
        "seed_hash",
        "inputs",
        "prestate",
        "targets",
        "desired",
        "physical_steps",
        "plan_hash",
    }
    if set(plan) != plan_keys:
        raise BootstrapError("runtime bootstrap plan has non-canonical keys")
    inputs = plan.get("inputs")
    if (
        not isinstance(inputs, Mapping)
        or set(inputs) != {"environment_manifest", "release_artifacts"}
        or not exact_json_equal(inputs.get("environment_manifest"), dict(environment_binding))
        or not exact_json_equal(inputs.get("release_artifacts"), dict(releases))
    ):
        raise BootstrapError("runtime bootstrap plan input binding is not exact")
    prestate = plan.get("prestate")
    if not isinstance(prestate, Mapping) or set(prestate) != {
        "probe",
        "old_bridge",
        "archive_root",
        "parent_directories",
    }:
        raise BootstrapError("runtime bootstrap prestate shape is invalid")
    backup = manifest.get("backup") if isinstance(manifest.get("backup"), Mapping) else {}
    backup_keys = {
        "probe": "probe",
        "old_bridge": "old_bridge",
        "archive_root": "bridge_archive_root",
    }
    for key, backup_key in backup_keys.items():
        record = prestate.get(key)
        if not isinstance(record, Mapping) or set(record) != {"snapshot", "identity"}:
            raise BootstrapError(f"runtime bootstrap {key} prestate is invalid")
        expected_snapshot = backup.get(backup_key, {}).get("snapshot")
        if not exact_json_equal(record.get("snapshot"), expected_snapshot):
            raise BootstrapError(f"runtime bootstrap {key} snapshot is not manifest-bound")
        validate_identity_contract(record.get("identity"), record["snapshot"], key)
    parent_directories = prestate.get("parent_directories")
    if not isinstance(parent_directories, Mapping) or set(parent_directories) != {
        "oni_root",
        "probe_container",
        "probe_parent",
    }:
        raise BootstrapError("runtime bootstrap parent-directory prestate is invalid")
    for label, value in parent_directories.items():
        if (
            not isinstance(value, Mapping)
            or set(value) != {"identity", "last_write_ns"}
            or not _valid_identity_list(value.get("identity"))
            or type(value.get("last_write_ns")) is not int
        ):
            raise BootstrapError(f"runtime bootstrap {label} custody is invalid")
    expected_targets = {
        "probe_relative": PROBE_RELATIVE.as_posix(),
        "bridge_relative": BRIDGE_RELATIVE.as_posix(),
        "archive_root_relative": ARCHIVE_ROOT_RELATIVE.as_posix(),
        "evidence_relative": f"{roots.run_id}/{TRANSACTION_RELATIVE.as_posix()}",
    }
    if not exact_json_equal(plan.get("targets"), expected_targets):
        raise BootstrapError("runtime bootstrap target set is not exact")
    seed = {
        "schema": "oni.live-runtime-bootstrap-seed.v1",
        "run_id": roots.run_id,
        "expected_mod_version": args.expected_mod_version,
        "inputs": dict(inputs),
        "prestate": dict(prestate),
        "targets": expected_targets,
    }
    seed_hash = canonical_hash(seed)
    transaction_id = "rtb-" + seed_hash.removeprefix("sha256:")[:24]
    if plan.get("seed_hash") != seed_hash or plan.get("transaction_id") != transaction_id:
        raise BootstrapError("runtime bootstrap deterministic transaction id is invalid")
    expected_desired = desired_documents(
        roots,
        manifest,
        releases[ARTIFACT_NAMES[0]]["sha256"],
        transaction_id,
        prestate["old_bridge"]["snapshot"].get("exists") is True,
        args.expected_mod_version,
    )
    expected_steps = [
        "stage_probe",
        "stage_bridge",
        "ensure_archive_root_if_needed",
        "archive_old_bridge_if_present",
        "recover_old_probe_if_present",
        "install_probe",
        "install_bridge",
    ]
    expected_plan = {
        "schema": PLAN_SCHEMA,
        "transaction_id": transaction_id,
        "run_id": roots.run_id,
        "expected_mod_version": args.expected_mod_version,
        "seed_hash": seed_hash,
        "inputs": dict(inputs),
        "prestate": dict(prestate),
        "targets": expected_targets,
        "desired": expected_desired,
        "physical_steps": expected_steps,
    }
    expected_plan["plan_hash"] = canonical_hash(expected_plan)
    if not exact_json_equal(dict(plan), expected_plan):
        raise BootstrapError("runtime bootstrap plan is not the deterministic canonical plan")
    expected_intent = {
        "schema": INTENT_SCHEMA,
        "transaction_id": transaction_id,
        "run_id": roots.run_id,
        "plan_hash": expected_plan["plan_hash"],
        "environment_manifest_sha256": environment_binding["sha256"],
        "release_dll_sha256": releases[ARTIFACT_NAMES[0]]["sha256"],
        "expected_mod_version": args.expected_mod_version,
        "confirm_live_run_id": args.confirm_live_run_id,
        "operation": "install_prebuilt_release",
    }
    expected_intent["intent_hash"] = canonical_hash(expected_intent)
    if not exact_json_equal(dict(intent), expected_intent):
        raise BootstrapError("runtime bootstrap intent is not canonical/exact")


def utf16_units(path: pathlib.Path) -> int:
    # Callers enumerate already-normalized absolute filesystem candidates.
    # Keep this helper a pure UTF-16 code-unit counter so boundary checks are
    # exact and independently testable for the empty and one-unit strings.
    return len(str(path).encode("utf-16-le")) // 2


def legacy_path_candidates(
    roots: Roots,
    plan: Mapping[str, Any],
    abort_plan: Optional[Mapping[str, Any]] = None,
    explicit: Optional[Mapping[str, pathlib.Path]] = None,
) -> list[pathlib.Path]:
    transaction = roots.transaction_dir
    candidates: list[pathlib.Path] = [
        roots.environment_dir / "environment-manifest.json",
        roots.environment_dir / "restore-journal.jsonl",
        transaction,
        transaction / "owner.json",
        transaction / "intent.json",
        transaction / "plan.json",
        transaction / "result-candidate.json",
        transaction / "result.json",
        transaction / "outcome-unknown.json",
        transaction / "transaction.lock",
        transaction / "stage",
        transaction / "stage" / "probe",
        transaction / "stage" / "bridge",
        transaction / "stage" / "archive-root",
        transaction / "recovery" / "old-probe",
        transaction / "environment-lock-attempts",
        roots.environment_dir / "runtime-bootstrap-install-stage.json",
        roots.environment_dir / "runtime-bootstrap-abort-stage.json",
        roots.environment_dir / "runtime-bootstrap-initialization-outcome-unknown.json",
        roots.environment_dir / ".finalize.lock",
    ]
    publication_paths = [
        transaction / "owner.json",
        transaction / "intent.json",
        transaction / "plan.json",
        transaction / "result-candidate.json",
        transaction / "result.json",
        transaction / "outcome-unknown.json",
        roots.environment_dir / "runtime-bootstrap-install-stage.json",
        roots.environment_dir / "runtime-bootstrap-abort-stage.json",
        roots.environment_dir / "runtime-bootstrap-initialization-outcome-unknown.json",
    ]
    for rank in range(12):
        claim_path = transaction.parent / f"runtime-bootstrap-init-{rank:02d}.json"
        candidates.append(claim_path)
        publication_paths.append(claim_path)
    attempt_examples = (
        (0, "install"),
        (1, "replay"),
        (2, "abort"),
        (9999, "install"),
        (9999, "replay"),
        (9999, "abort"),
    )
    for epoch, purpose in attempt_examples:
        attempt = transaction / "environment-lock-attempts" / f"{epoch:04d}-{purpose}"
        candidates.append(attempt)
        attempt_parent_claim = attempt.with_name(attempt.name + ".claim.json")
        candidates.append(attempt_parent_claim)
        publication_paths.append(attempt_parent_claim)
        for name in (
            "owner.json",
            "intent.json",
            "lock.stage",
            "claim.json",
            "acquired.json",
            "released.lock",
            "released.json",
        ):
            path = attempt / name
            candidates.append(path)
            if name not in {"released.lock"}:
                publication_paths.append(path)
    for event in JOURNAL_EVENT_ORDER:
        journal_publication = (
            transaction / "journal" / f"{JOURNAL_EVENT_ORDER[event]:04d}-{event}.json"
        )
        candidates.append(journal_publication)
        publication_paths.append(journal_publication)
    candidates.extend(path.with_name(path.name + ".new") for path in publication_paths)
    release_parent = normalized(pathlib.Path(str(plan["desired"]["bootstrap"]["bridge_dir"]))).parent
    del release_parent  # The release paths themselves are checked from the plan IDs/stable reads.
    live_probe = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
    live_bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    for name in ARTIFACT_NAMES:
        candidates.extend(
            (
                live_probe / name,
                transaction / "stage" / "probe" / name,
                transaction / "stage" / "probe" / (name + ".new"),
            )
        )
    for relative in (
        "action-policy.json",
        "validation-control-policy.json",
        "v27-runtime-bootstrap.json",
    ):
        candidates.extend(
            (
                live_bridge / relative,
                transaction / "stage" / "bridge" / relative,
                transaction / "stage" / "bridge" / (relative + ".new"),
            )
        )
    for queue in COMMAND_QUEUES:
        candidates.append(live_bridge / "commands" / queue)
    for queue in CONTROL_QUEUES:
        candidates.append(live_bridge / "validation-controls" / queue)
    archive_relative = plan.get("desired", {}).get("archive_relative")
    if archive_relative:
        archive = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(archive_relative).parts)
        candidates.append(archive)
        for item in plan["prestate"]["old_bridge"]["snapshot"].get("entries", []):
            candidates.append(archive / pathlib.Path(*pathlib.PurePosixPath(item["relative"]).parts))
    recovery = transaction / "recovery" / "old-probe"
    for item in plan["prestate"]["probe"]["snapshot"].get("entries", []):
        candidates.append(recovery / pathlib.Path(*pathlib.PurePosixPath(item["relative"]).parts))
    abort_root = transaction / "abort"
    candidates.extend(
        (
            abort_root,
            abort_root / "custody",
            abort_root / "journal",
            abort_root / "owner.json",
            abort_root / "intent.json",
            abort_root / "plan.json",
            abort_root / "result-candidate.json",
            abort_root / "result.json",
            abort_root / "outcome-unknown.json",
            abort_root / "custody" / "b",
            abort_root / "custody" / "p",
            abort_root / "custody" / "a",
        )
    )
    abort_runtime_probe = abort_root / "custody" / "p"
    for name in ARTIFACT_NAMES:
        candidates.append(abort_runtime_probe / name)
    abort_runtime_bridge = abort_root / "custody" / "b"
    for relative in (
        "action-policy.json",
        "validation-control-policy.json",
        "v27-runtime-bootstrap.json",
        "commands",
        *(f"commands/{queue}" for queue in COMMAND_QUEUES),
        "validation-controls",
        *(f"validation-controls/{queue}" for queue in CONTROL_QUEUES),
        "evidence",
    ):
        candidates.append(
            abort_runtime_bridge
            / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        )
    if isinstance(archive_relative, str) and archive_relative:
        future_abort_archive = (
            abort_root
            / "custody"
            / "a"
            / pathlib.PurePosixPath(archive_relative).name
        )
        candidates.append(future_abort_archive)
        for item in (
            plan.get("prestate", {})
            .get("old_bridge", {})
            .get("snapshot", {})
            .get("entries", [])
        ):
            candidates.append(
                future_abort_archive
                / pathlib.Path(
                    *pathlib.PurePosixPath(str(item.get("relative"))).parts
                )
            )
    publication_paths.extend(
        (
            abort_root / "owner.json",
            abort_root / "intent.json",
            abort_root / "plan.json",
            abort_root / "result-candidate.json",
            abort_root / "result.json",
            abort_root / "outcome-unknown.json",
        )
    )
    if abort_plan is not None:
        events = _abort_journal_events(abort_plan)
        for index, event in enumerate(events):
            path = abort_root / "journal" / f"{index:04d}-{event}.json"
            candidates.append(path)
            publication_paths.append(path)
        if explicit is not None:
            for path in _abort_role_paths(roots, abort_plan, explicit).values():
                candidates.append(path)
        for prestate_key, custody_name in (
            ("old_bridge", "a"),
            ("probe", "p"),
        ):
            for item in plan.get("prestate", {}).get(prestate_key, {}).get("snapshot", {}).get("entries", []):
                prefix = abort_root / "custody" / custody_name
                if prestate_key == "old_bridge" and abort_plan.get("archive_name"):
                    prefix = prefix / str(abort_plan["archive_name"])
                candidates.append(
                    prefix
                    / pathlib.Path(*pathlib.PurePosixPath(str(item.get("relative"))).parts)
                )
    candidates.extend(path.with_name(path.name + ".new") for path in publication_paths)
    return sorted(set(candidates), key=lambda value: str(value).casefold())


def _legacy_path_disclosure(
    path: pathlib.Path,
    roots: Roots,
) -> dict[str, Any]:
    """Return a path-budget view bounded by the manifest disclosure contract."""

    for root_name, root in (
        ("oni_root", roots.oni_root),
        ("evidence_root", roots.evidence_root),
    ):
        if path_within(path, root, strict=False):
            try:
                relative = path.relative_to(root)
            except ValueError:
                # Generated candidates are normally direct descendants.  Keep
                # the diagnostic fail-closed if a future path representation
                # differs in case or normalization, without exposing a root.
                relative = pathlib.Path(os.path.relpath(str(path), str(root)))
            return {
                "root": root_name,
                "relative_path": pathlib.PurePosixPath(relative).as_posix(),
                "path_id_lower": path_id(path),
            }
    # This should be unreachable for the current candidate builder.  Do not
    # turn a future builder mistake into an absolute-path disclosure.
    return {
        "root": "unbound",
        "relative_path": None,
        "path_id_lower": path_id(path),
    }


def legacy_path_budget_diagnostic(
    roots: Roots,
    candidates: Sequence[pathlib.Path],
) -> dict[str, Any]:
    """Build deterministic, contract-safe details for an over-budget refusal."""

    offending = sorted(
        (path for path in candidates if utf16_units(path) > 259),
        key=lambda value: str(value).casefold(),
    )
    disclosed: list[dict[str, Any]] = []
    for path in offending:
        item = _legacy_path_disclosure(path, roots)
        units = utf16_units(path)
        item.update(
            {
                "utf16_units": units,
                "over_limit_by": units - 259,
            }
        )
        disclosed.append(item)
    return {
        "schema": "oni.live-runtime-bootstrap-path-budget.v1",
        "path_disclosure": "root_ids_and_relative_paths_only",
        "limit_units": 259,
        "candidate_count": len(candidates),
        "offending_count": len(disclosed),
        "offending_candidates": disclosed,
        "offending_candidates_hash": canonical_hash(disclosed),
    }


def validate_legacy_path_budget(
    roots: Roots,
    plan: Mapping[str, Any],
    abort_plan: Optional[Mapping[str, Any]] = None,
    explicit: Optional[Mapping[str, pathlib.Path]] = None,
) -> None:
    candidates = legacy_path_candidates(roots, plan, abort_plan, explicit)
    if any(utf16_units(path) > 259 for path in candidates):
        raise BootstrapError(
            "runtime bootstrap path exceeds the 259 UTF-16-unit legacy budget",
            diagnostic=legacy_path_budget_diagnostic(roots, candidates),
        )


def checkpoint(
    args: argparse.Namespace,
    point: str,
    hook: Optional[Callable[[str], None]],
) -> None:
    if hook is not None:
        hook(point)
    if args.test_fail_at == point:
        if not args.test_root:
            raise BootstrapError("fault injection is TestRoot-only")
        raise InjectedCrash(point)


def _initialization_intent(
    roots: Roots,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    stage_nonce: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{32}", stage_nonce) is None:
        raise BootstrapError("runtime bootstrap stage owner token is invalid")
    producer = stable_file(pathlib.Path(__file__).resolve(), max_bytes=MAX_JSON_BYTES, collect=False)
    return _seal_contract_document(
        {
            "schema": INSTALL_STAGE_SCHEMA,
            "run_id": roots.run_id,
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "install_intent_hash": intent["intent_hash"],
            "transaction_path_id_lower": path_id(roots.transaction_dir),
            "stage_nonce": stage_nonce,
            "install_intent": dict(intent),
            "install_plan": dict(plan),
            "producer": {
                "length": producer["length"],
                "sha256": producer["sha256"],
            },
        },
        "stage_hash",
    )


def _load_initialization_intent(
    roots: Roots,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    expected_file_sha256: Optional[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = roots.environment_dir / "runtime-bootstrap-install-stage.json"
    value, state = stable_json(path, "runtime bootstrap stage intent")
    token = value.get("stage_nonce")
    if not isinstance(token, str):
        raise BootstrapError("runtime bootstrap stage intent owner token is missing")
    expected = _initialization_intent(roots, intent, plan, token)
    if not exact_json_equal(value, expected):
        raise BootstrapError("runtime bootstrap stage intent is not exact")
    if expected_file_sha256 is not None and state.get("sha256") != require_hash(
        expected_file_sha256, "external bootstrap stage-intent hash"
    ):
        raise BootstrapError("runtime bootstrap stage intent does not match its external hash")
    return expected, state


def _initialization_claim(
    init_intent: Mapping[str, Any],
    *,
    rank: int,
    relative: str,
    kind: str,
    identity: Sequence[int],
    payload_sha256: Optional[str],
) -> dict[str, Any]:
    return _seal_contract_document(
        {
            "schema": INIT_CLAIM_SCHEMA,
            "transaction_id": init_intent["transaction_id"],
            "plan_hash": init_intent["plan_hash"],
            "stage_hash": init_intent["stage_hash"],
            "rank": rank,
            "relative": relative,
            "kind": kind,
            "identity": list(identity),
            "payload_sha256": payload_sha256,
        },
        "claim_hash",
    )


def _initialization_latch_path(roots: Roots) -> pathlib.Path:
    return roots.environment_dir / "runtime-bootstrap-initialization-outcome-unknown.json"


def _initialization_latch_present(roots: Roots) -> bool:
    path = _initialization_latch_path(roots)
    return os.path.lexists(path) or os.path.lexists(path.with_name(path.name + ".new"))


def _initialization_support_specs(
    roots: Roots,
) -> tuple[tuple[int, pathlib.Path, str], ...]:
    transaction = roots.transaction_dir
    return (
        (0, transaction, "directory"),
        (1, transaction / "transaction.lock", "file"),
        (2, transaction / "stage", "directory"),
        (3, transaction / "stage" / "probe", "directory"),
        (4, transaction / "stage" / "bridge", "directory"),
        (5, transaction / "stage" / "archive-root", "directory"),
        (6, transaction / "recovery", "directory"),
        (7, transaction / "environment-lock-attempts", "directory"),
        (8, transaction / "journal", "directory"),
        (9, transaction / "abort", "directory"),
        (10, transaction / "abort" / "custody", "directory"),
        (11, transaction / "abort" / "journal", "directory"),
    )


def _initialization_artifact_present(roots: Roots) -> bool:
    run_root = roots.transaction_dir.parent
    if os.path.lexists(roots.transaction_dir):
        return True
    for rank, _path, _kind in _initialization_support_specs(roots):
        claim = run_root / f"runtime-bootstrap-init-{rank:02d}.json"
        if os.path.lexists(claim) or os.path.lexists(
            claim.with_name(claim.name + ".new")
        ):
            return True
    return False


def _initialization_node_observation(path: pathlib.Path) -> dict[str, Any]:
    try:
        if not os.path.lexists(path):
            return {"status": "absent"}
        metadata = os.lstat(path)
        if is_reparse_metadata(metadata):
            kind = "reparse"
        elif stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
        else:
            kind = "other"
        return {
            "status": "present",
            "kind": kind,
            "identity": node_identity(metadata),
            "link_count": int(metadata.st_nlink),
            "length": int(metadata.st_size),
        }
    except OSError:
        return {"status": "unreadable_or_changed"}


def _write_refusal_latch_create_only(
    path: pathlib.Path,
    value: Mapping[str, Any],
    label: str,
) -> None:
    """Publish a fail-closed latch without ever adopting a pre-existing inode.

    Unlike ordinary recoverable publications, this writes directly to the
    final O_EXCL name.  A power loss can therefore leave a partial final file,
    but the name alone is an authorization barrier on every later invocation.
    No existing final or ``.new`` inode, even one with identical bytes, is
    accepted as ours.
    """

    path = normalized(path)
    temporary = path.with_name(path.name + ".new")
    if os.path.lexists(path) or os.path.lexists(temporary):
        raise BootstrapError(f"{label} already exists or has a partial publication")
    payload = document_bytes(value)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise BootstrapError(f"{label} appeared concurrently") from exc
    identity: Optional[list[int]] = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                is_reparse_metadata(opened)
                or not stat.S_ISREG(opened.st_mode)
                or int(opened.st_nlink) != 1
            ):
                raise BootstrapError(f"{label} final inode is unsafe")
            identity = node_identity(opened)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            after = os.fstat(handle.fileno())
            if node_identity(after) != identity or int(after.st_nlink) != 1:
                raise BootstrapError(f"{label} final inode changed during write")
    except BaseException:
        # The create-only final name deliberately remains a permanent refusal
        # barrier even when only a prefix reached disk.
        raise
    if identity is None:
        raise BootstrapError(f"{label} final identity was not captured")
    published = stable_file(path, max_bytes=max(MAX_JSON_BYTES, len(payload)), collect=True)
    if published.get("identity") != identity or published.get("bytes") != payload:
        raise BootstrapError(f"{label} final publication verification failed")


def _publish_initialization_outcome_unknown(
    args: argparse.Namespace,
    roots: Roots,
    stage: Mapping[str, Any],
    stage_state: Mapping[str, Any],
    plan: Mapping[str, Any],
    error: BaseException,
    hook: Optional[Callable[[str], None]],
) -> None:
    path = _initialization_latch_path(roots)
    if _initialization_latch_present(roots):
        raise BootstrapError(
            "runtime bootstrap initialization outcome_unknown latch already exists"
        )
    run_root = roots.transaction_dir.parent
    support: list[dict[str, Any]] = []
    for rank, node, expected_kind in _initialization_support_specs(roots):
        claim = run_root / f"runtime-bootstrap-init-{rank:02d}.json"
        support.append(
            {
                "rank": rank,
                "relative": node.relative_to(run_root).as_posix(),
                "expected_kind": expected_kind,
                "node": _initialization_node_observation(node),
                "claim": _initialization_node_observation(claim),
                "claim_partial": _initialization_node_observation(
                    claim.with_name(claim.name + ".new")
                ),
            }
        )
    latch: dict[str, Any] = {
        "schema": INIT_OUTCOME_UNKNOWN_SCHEMA,
        "status": "outcome_unknown",
        "classification": "infra_fail",
        "run_id": roots.run_id,
        "transaction_id": plan.get("transaction_id"),
        "plan_hash": plan.get("plan_hash"),
        "install_stage_sha256": stage_state.get("sha256"),
        "install_stage_hash": stage.get("stage_hash"),
        "support_nodes": support,
        "failure_type": type(error).__name__,
        "failure_hash": sha256_bytes(str(error).encode("utf-8", errors="replace")),
        "manual_recovery_required": True,
        "runtime_mutation_started": False,
        "game_launched": False,
    }
    latch["latch_hash"] = canonical_hash(latch)
    _write_refusal_latch_create_only(
        path,
        latch,
        "runtime bootstrap initialization outcome_unknown latch",
    )
    checkpoint(args, "after_initialization_outcome_unknown_publish", hook)


def _ensure_initialization_node(
    args: argparse.Namespace,
    roots: Roots,
    init_intent: Mapping[str, Any],
    *,
    rank: int,
    path: pathlib.Path,
    kind: str,
    payload: Optional[bytes],
    checkpoint_name: str,
    hook: Optional[Callable[[str], None]],
) -> list[int]:
    if kind not in {"file", "directory"} or (kind == "file") != (payload is not None):
        raise BootstrapError("runtime bootstrap initialization node contract is invalid")
    run_root = roots.transaction_dir.parent
    try:
        relative = path.relative_to(run_root).as_posix()
    except ValueError as exc:
        raise BootstrapError("runtime bootstrap initialization node escaped its run root") from exc
    claim_path = run_root / f"runtime-bootstrap-init-{rank:02d}.json"
    claim: Optional[dict[str, Any]] = None
    if os.path.lexists(claim_path):
        claim, _ = stable_json(claim_path, "runtime bootstrap initialization claim")
        if (
            set(claim)
            != {
                "schema",
                "transaction_id",
                "plan_hash",
                "stage_hash",
                "rank",
                "relative",
                "kind",
                "identity",
                "payload_sha256",
                "claim_hash",
            }
            or claim.get("schema") != INIT_CLAIM_SCHEMA
            or claim.get("transaction_id") != init_intent["transaction_id"]
            or claim.get("plan_hash") != init_intent["plan_hash"]
            or claim.get("stage_hash") != init_intent["stage_hash"]
            or claim.get("rank") != rank
            or claim.get("relative") != relative
            or claim.get("kind") != kind
            or not _valid_identity_list(claim.get("identity"))
            or claim.get("payload_sha256")
            != (sha256_bytes(payload) if payload is not None else None)
            or canonical_hash({key: value for key, value in claim.items() if key != "claim_hash"})
            != claim.get("claim_hash")
        ):
            raise BootstrapError("runtime bootstrap initialization claim is invalid")
    if not os.path.lexists(path):
        if claim is not None:
            raise BootstrapError("claimed runtime bootstrap initialization node disappeared")
        if kind == "directory":
            try:
                path.mkdir()
            except FileExistsError as exc:
                raise BootstrapError("runtime bootstrap initialization node appeared concurrently") from exc
            checkpoint(
                args,
                checkpoint_name + "_physical_before_claim",
                hook,
            )
        else:
            assert payload is not None
            try:
                handle = path.open("x+b")
            except FileExistsError as exc:
                raise BootstrapError("runtime bootstrap initialization file appeared concurrently") from exc
            try:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
                    raise BootstrapError("runtime bootstrap initialization file is unsafe")
                identity = node_identity(metadata)
                expected = _initialization_claim(
                    init_intent,
                    rank=rank,
                    relative=relative,
                    kind=kind,
                    identity=identity,
                    payload_sha256=sha256_bytes(payload),
                )
                checkpoint(
                    args,
                    checkpoint_name + "_physical_before_claim",
                    hook,
                )
                write_json_create_only(
                    claim_path,
                    expected,
                    "runtime bootstrap initialization claim",
                )
                checkpoint(args, checkpoint_name + "_prewrite", hook)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                if node_identity(os.fstat(handle.fileno())) != identity:
                    raise BootstrapError("runtime bootstrap initialization file changed during write")
            finally:
                handle.close()
            claim = expected
    elif claim is None:
        # Existing content without a durable final inode claim is never
        # adopted, even when empty or byte-for-byte identical.
        raise BootstrapError("unclaimed runtime bootstrap initialization node is outcome_unknown")
    metadata = os.lstat(path)
    if (
        is_reparse_metadata(metadata)
        or (kind == "directory" and not stat.S_ISDIR(metadata.st_mode))
        or (
            kind == "file"
            and (not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1)
        )
    ):
        raise BootstrapError("runtime bootstrap initialization node is unsafe")
    if kind == "file":
        assert payload is not None
        state = stable_file(path, max_bytes=max(MAX_JSON_BYTES, len(payload)), collect=True)
        if state.get("bytes") != payload:
            raise BootstrapError("runtime bootstrap initialization file payload changed")
        identity = state["identity"]
    else:
        identity = node_identity(metadata)
    expected = _initialization_claim(
        init_intent,
        rank=rank,
        relative=relative,
        kind=kind,
        identity=identity,
        payload_sha256=sha256_bytes(payload) if payload is not None else None,
    )
    if claim is None:
        write_json_create_only(claim_path, expected, "runtime bootstrap initialization claim")
    elif not exact_json_equal(claim, expected):
        raise BootstrapError("runtime bootstrap initialization node was name-swapped")
    checkpoint(args, checkpoint_name, hook)
    return list(identity)


def initialize_transaction(
    args: argparse.Namespace,
    roots: Roots,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    init_intent: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> None:
    transaction = roots.transaction_dir
    loaded_init, _ = _load_initialization_intent(
        roots, intent, plan, expected_file_sha256=None
    )
    if not exact_json_equal(dict(init_intent), loaded_init):
        raise BootstrapError("runtime bootstrap initialization authorization changed")
    checkpoint(args, "after_initialization_intent_publish", hook)
    if os.path.lexists(transaction) and os.path.lexists(transaction / "owner.json"):
        if is_reparse(transaction) or not transaction.is_dir():
            raise BootstrapError("runtime bootstrap latch was externally replaced")
        owner_path = transaction / "owner.json"
        owner, _ = stable_json(owner_path, "runtime bootstrap owner")
        owner_keys = {
            "schema",
            "run_id",
            "transaction_id",
            "root_identity",
            "lock_identity",
            "stage_root_identity",
            "probe_stage_identity",
            "bridge_stage_identity",
            "archive_stage_identity",
            "recovery_root_identity",
            "lock_attempt_root_identity",
            "journal_identity",
            "abort_root_identity",
            "abort_custody_identity",
            "abort_journal_identity",
            "stage_hash",
        }
        if (
            set(owner) != owner_keys
            or owner.get("schema") != OWNER_SCHEMA
            or owner.get("run_id") != roots.run_id
            or owner.get("transaction_id") != plan["transaction_id"]
            or owner.get("stage_hash") != init_intent["stage_hash"]
            or owner.get("root_identity") != node_identity(os.lstat(transaction))
            or any(
                not _valid_identity_list(owner.get(key))
                for key in owner_keys
                if key.endswith("_identity")
            )
        ):
            raise BootstrapError("runtime bootstrap latch ownership is invalid")
        owned_paths = {
            "lock_identity": (transaction / "transaction.lock", "file"),
            "stage_root_identity": (transaction / "stage", "directory"),
            "probe_stage_identity": (transaction / "stage" / "probe", "directory"),
            "bridge_stage_identity": (transaction / "stage" / "bridge", "directory"),
            "archive_stage_identity": (transaction / "stage" / "archive-root", "directory"),
            "recovery_root_identity": (transaction / "recovery", "directory"),
            "lock_attempt_root_identity": (
                transaction / "environment-lock-attempts",
                "directory",
            ),
            "journal_identity": (transaction / "journal", "directory"),
            "abort_root_identity": (transaction / "abort", "directory"),
            "abort_custody_identity": (
                transaction / "abort" / "custody",
                "directory",
            ),
            "abort_journal_identity": (
                transaction / "abort" / "journal",
                "directory",
            ),
        }
        for key, (owned_path, kind) in owned_paths.items():
            if not os.path.lexists(owned_path):
                # Stage children are legitimately moved away after commit.
                if key in {"probe_stage_identity", "bridge_stage_identity", "archive_stage_identity"}:
                    continue
                raise BootstrapError("runtime bootstrap owned support path disappeared")
            metadata = os.lstat(owned_path)
            if (
                is_reparse_metadata(metadata)
                or (kind == "file" and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1))
                or (kind == "directory" and not stat.S_ISDIR(metadata.st_mode))
                or owner.get(key) != node_identity(metadata)
            ):
                raise BootstrapError("runtime bootstrap owned support path was name-swapped")
    else:
        assert_no_reparse(transaction.parent, roots.evidence_root)
        root_identity = _ensure_initialization_node(
            args,
            roots,
            init_intent,
            rank=0,
            path=transaction,
            kind="directory",
            payload=None,
            checkpoint_name="after_transaction_root_claim",
            hook=hook,
        )
        lock_path = transaction / "transaction.lock"
        lock_identity = _ensure_initialization_node(
            args,
            roots,
            init_intent,
            rank=1,
            path=lock_path,
            kind="file",
            payload=b"\0",
            checkpoint_name="after_transaction_lock_claim",
            hook=hook,
        )
        stage_root = transaction / "stage"
        stage_root_identity = _ensure_initialization_node(
            args,
            roots,
            init_intent,
            rank=2,
            path=stage_root,
            kind="directory",
            payload=None,
            checkpoint_name="after_stage_root_claim",
            hook=hook,
        )
        probe_stage = stage_root / "probe"
        bridge_stage = stage_root / "bridge"
        archive_stage = stage_root / "archive-root"
        recovery_root = transaction / "recovery"
        lock_attempt_root = transaction / "environment-lock-attempts"
        journal = transaction / "journal"
        abort_root = transaction / "abort"
        abort_custody = abort_root / "custody"
        abort_journal = abort_root / "journal"
        support_nodes = (
            (3, probe_stage, "after_probe_stage_root_claim"),
            (4, bridge_stage, "after_bridge_stage_root_claim"),
            (5, archive_stage, "after_archive_stage_root_claim"),
            (6, recovery_root, "after_recovery_root_claim"),
            (7, lock_attempt_root, "after_lock_attempt_root_claim"),
            (8, journal, "after_journal_root_claim"),
            (9, abort_root, "after_abort_support_root_claim"),
            (10, abort_custody, "after_abort_custody_root_claim"),
            (11, abort_journal, "after_abort_journal_root_claim"),
        )
        support_identities: dict[pathlib.Path, list[int]] = {}
        for rank, directory, point in support_nodes:
            support_identities[directory] = _ensure_initialization_node(
                args,
                roots,
                init_intent,
                rank=rank,
                path=directory,
                kind="directory",
                payload=None,
                checkpoint_name=point,
                hook=hook,
            )
        owner = {
            "schema": OWNER_SCHEMA,
            "run_id": roots.run_id,
            "transaction_id": plan["transaction_id"],
            "root_identity": root_identity,
            "lock_identity": lock_identity,
            "stage_root_identity": stage_root_identity,
            "probe_stage_identity": support_identities[probe_stage],
            "bridge_stage_identity": support_identities[bridge_stage],
            "archive_stage_identity": support_identities[archive_stage],
            "recovery_root_identity": support_identities[recovery_root],
            "lock_attempt_root_identity": support_identities[lock_attempt_root],
            "journal_identity": support_identities[journal],
            "abort_root_identity": support_identities[abort_root],
            "abort_custody_identity": support_identities[abort_custody],
            "abort_journal_identity": support_identities[abort_journal],
            "stage_hash": init_intent["stage_hash"],
        }
        write_json_create_only(transaction / "owner.json", owner, "runtime bootstrap owner")
        checkpoint(args, "after_owner_publish", hook)
    write_json_create_only(transaction / "intent.json", intent, "runtime bootstrap intent")
    checkpoint(args, "after_intent_publish", hook)
    write_json_create_only(transaction / "plan.json", plan, "runtime bootstrap plan")
    checkpoint(args, "after_plan_publish", hook)
    journal = transaction / "journal"
    if is_reparse(journal) or not journal.is_dir():
        raise BootstrapError("runtime bootstrap journal was externally replaced")
    publications = {
        "owner": file_record(transaction / "owner.json")["identity"],
        "intent": file_record(transaction / "intent.json")["identity"],
        "plan": file_record(transaction / "plan.json")["identity"],
    }
    append_journal(
        transaction,
        "transaction_initialized",
        {
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "publication_identities": publications,
        },
    )
    checkpoint(args, "after_journal_initialized", hook)


def load_published_transaction(
    roots: Roots,
) -> tuple[dict[str, Any], dict[str, Any]]:
    owner, _ = stable_json(roots.transaction_dir / "owner.json", "runtime bootstrap owner")
    if owner.get("root_identity") != node_identity(os.lstat(roots.transaction_dir)):
        raise BootstrapError("runtime bootstrap transaction root identity changed")
    intent, _ = stable_json(roots.transaction_dir / "intent.json", "runtime bootstrap intent")
    plan, _ = stable_json(roots.transaction_dir / "plan.json", "runtime bootstrap plan")
    validate_plan_shape(plan)
    validate_intent_shape(intent, plan)
    if owner.get("transaction_id") != plan.get("transaction_id"):
        raise BootstrapError("runtime bootstrap owner/plan transaction mismatch")
    entries = verify_journal(roots.transaction_dir)
    publications = entries[0]["data"]["publication_identities"]
    for key, filename in (("owner", "owner.json"), ("intent", "intent.json"), ("plan", "plan.json")):
        if node_identity(os.lstat(roots.transaction_dir / filename)) != publications[key]:
            raise BootstrapError("runtime bootstrap immutable publication was name-swapped")
    return intent, plan


def journal_path(transaction: pathlib.Path, index: int, event: str) -> pathlib.Path:
    return transaction / "journal" / f"{index:04d}-{event}.json"


def verify_journal(transaction: pathlib.Path) -> list[dict[str, Any]]:
    journal = transaction / "journal"
    if not journal.exists() or is_reparse(journal) or not journal.is_dir():
        raise BootstrapError("runtime bootstrap journal is missing/unsafe")
    entries: list[dict[str, Any]] = []
    previous: Optional[str] = None
    files = sorted(journal.iterdir(), key=lambda path: path.name)
    plan, _ = stable_json(transaction / "plan.json", "runtime bootstrap journal plan")
    transaction_id = plan.get("transaction_id")
    plan_hash = plan.get("plan_hash")
    seen_events: set[str] = set()
    prior_rank = -1
    for index, path in enumerate(files):
        if not re.fullmatch(r"\d{4}-[a-z0-9_-]+\.json", path.name):
            raise BootstrapError("runtime bootstrap journal contains an unexpected entry")
        entry, _ = stable_json(path, "runtime bootstrap journal entry")
        if (
            set(entry)
            != {
                "schema",
                "index",
                "transaction_id",
                "plan_hash",
                "event",
                "data",
                "previous_entry_hash",
                "entry_hash",
            }
            or entry.get("schema") != JOURNAL_SCHEMA
            or entry.get("index") != index
            or entry.get("transaction_id") != transaction_id
            or entry.get("plan_hash") != plan_hash
            or entry.get("previous_entry_hash") != previous
            or not isinstance(entry.get("event"), str)
            or not isinstance(entry.get("data"), Mapping)
        ):
            raise BootstrapError("runtime bootstrap journal chain is invalid")
        event = str(entry["event"])
        rank = JOURNAL_EVENT_ORDER.get(event)
        if rank is None or event in seen_events or rank <= prior_rank:
            raise BootstrapError("runtime bootstrap journal event order is invalid")
        if index == 0 and event != "transaction_initialized":
            raise BootstrapError("runtime bootstrap journal has no initialization origin")
        validate_journal_data(event, entry["data"], plan)
        unhashed = dict(entry)
        claimed = unhashed.pop("entry_hash", None)
        if canonical_hash(unhashed) != claimed:
            raise BootstrapError("runtime bootstrap journal entry hash is invalid")
        expected_name = f"{index:04d}-{entry['event']}.json"
        if path.name != expected_name:
            raise BootstrapError("runtime bootstrap journal filename is invalid")
        previous = claimed
        prior_rank = rank
        seen_events.add(event)
        entries.append(entry)
    return entries


def append_journal(
    transaction: pathlib.Path,
    event: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    entries = verify_journal(transaction) if (transaction / "journal").exists() else []
    for entry in entries:
        if entry.get("event") == event:
            if not exact_json_equal(dict(entry.get("data", {})), dict(data)):
                raise BootstrapError(f"journal event {event} conflicts with its immutable data")
            return entry
    event_rank = JOURNAL_EVENT_ORDER.get(event)
    if event_rank is None:
        raise BootstrapError("unknown runtime bootstrap journal event")
    if entries and event_rank <= JOURNAL_EVENT_ORDER[str(entries[-1]["event"])]:
        raise BootstrapError("runtime bootstrap journal transition is out of order")
    index = len(entries)
    plan, _ = stable_json(transaction / "plan.json", "runtime bootstrap journal plan")
    entry: dict[str, Any] = {
        "schema": JOURNAL_SCHEMA,
        "index": index,
        "transaction_id": plan.get("transaction_id"),
        "plan_hash": plan.get("plan_hash"),
        "event": event,
        "data": dict(data),
        "previous_entry_hash": entries[-1]["entry_hash"] if entries else None,
    }
    entry["entry_hash"] = canonical_hash(entry)
    write_json_create_only(journal_path(transaction, index, event), entry, f"journal {event}")
    return entry


def _valid_identity_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(type(item) is int and item >= 0 for item in value)
    )


def _valid_identity_tree(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("exists") is not True or set(value) != {"exists", "root_identity", "entries"}:
        return False
    if not _valid_identity_list(value.get("root_identity")) or not isinstance(value.get("entries"), list):
        return False
    for item in value["entries"]:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"relative", "kind", "identity"}
            or item.get("kind") not in {"file", "directory"}
            or not isinstance(item.get("relative"), str)
            or not _valid_identity_list(item.get("identity"))
        ):
            return False
    return True


def validate_journal_data(event: str, data: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    keys = set(data)
    if event == "transaction_initialized":
        publications = data.get("publication_identities")
        if (
            keys != {"transaction_id", "plan_hash", "publication_identities"}
            or data.get("transaction_id") != plan.get("transaction_id")
            or data.get("plan_hash") != plan.get("plan_hash")
            or not isinstance(publications, Mapping)
            or set(publications) != {"owner", "intent", "plan"}
            or not all(_valid_identity_list(value) for value in publications.values())
        ):
            raise BootstrapError("transaction initialization journal data is invalid")
        return
    if event in {"probe_staged", "bridge_staged"}:
        if keys != {"content_hash", "identity"} or HASH_RE.fullmatch(
            str(data.get("content_hash") or "")
        ) is None or not _valid_identity_tree(data.get("identity")):
            raise BootstrapError(f"{event} journal data is invalid")
        return
    if event == "archive_root_prepared":
        if keys != {"identity"} or not _valid_identity_tree(data.get("identity")):
            raise BootstrapError("archive-root prepared journal data is invalid")
        return
    if event == "archive_root_ready":
        if (
            keys != {"created", "root_identity"}
            or type(data.get("created")) is not bool
            or not _valid_identity_list(data.get("root_identity"))
        ):
            raise BootstrapError("archive-root ready journal data is invalid")
        return
    if event == "old_bridge_archived":
        if data.get("present") is False:
            if not exact_json_equal(dict(data), {"present": False, "archive_relative": None}):
                raise BootstrapError("absent old-Bridge journal data is invalid")
        elif (
            data.get("present") is not True
            or keys
            != {
                "present",
                "archive_relative",
                "archive_identity",
                "archive_root_last_write_ns",
            }
            or data.get("archive_relative") != plan.get("desired", {}).get("archive_relative")
            or not _valid_identity_tree(data.get("archive_identity"))
            or type(data.get("archive_root_last_write_ns")) is not int
        ):
            raise BootstrapError("present old-Bridge journal data is invalid")
        return
    if event == "old_probe_recovered":
        if data.get("present") is False:
            if not exact_json_equal(dict(data), {"present": False, "recovery_relative": None}):
                raise BootstrapError("absent old-Probe journal data is invalid")
        elif (
            data.get("present") is not True
            or keys != {"present", "recovery_relative", "recovery_identity"}
            or data.get("recovery_relative") != "recovery/old-probe"
            or not _valid_identity_tree(data.get("recovery_identity"))
        ):
            raise BootstrapError("present old-Probe journal data is invalid")
        return
    if event in {"new_probe_installed", "new_bridge_installed"}:
        expected_relative = (
            PROBE_RELATIVE.as_posix() if event == "new_probe_installed" else BRIDGE_RELATIVE.as_posix()
        )
        if (
            keys != {"live_relative", "content_hash", "identity"}
            or data.get("live_relative") != expected_relative
            or HASH_RE.fullmatch(str(data.get("content_hash") or "")) is None
            or not _valid_identity_tree(data.get("identity"))
        ):
            raise BootstrapError(f"{event} journal data is invalid")
        return
    if event == "commit_verified":
        expected_keys = {
            "probe_tree_content_hash",
            "bridge_tree_content_hash",
            "archive_relative",
            "old_probe_recovery",
        }
        if (
            keys != expected_keys
            or HASH_RE.fullmatch(str(data.get("probe_tree_content_hash") or "")) is None
            or HASH_RE.fullmatch(str(data.get("bridge_tree_content_hash") or "")) is None
            or data.get("archive_relative") != plan.get("desired", {}).get("archive_relative")
            or data.get("old_probe_recovery")
            not in {None, "recovery/old-probe"}
        ):
            raise BootstrapError("commit verification journal data is invalid")
        return
    raise BootstrapError("runtime bootstrap journal event grammar is unknown")


def journal_event(transaction: pathlib.Path, event: str) -> Optional[dict[str, Any]]:
    for entry in verify_journal(transaction):
        if entry.get("event") == event:
            return entry
    return None


def _initialization_origin_complete(
    roots: Roots,
    plan: Mapping[str, Any],
) -> bool:
    """Recognize only the fully published, immutable initialization origin.

    This deliberately validates just the origin entry instead of the entire
    install journal.  A later malformed committed entry must therefore be
    classified as an install outcome-unknown, while an unclaimed support node
    (including an empty journal directory) remains an initialization
    outcome-unknown.
    """

    transaction = roots.transaction_dir
    try:
        owner_path = transaction / "owner.json"
        intent_path = transaction / "intent.json"
        plan_path = transaction / "plan.json"
        origin_path = transaction / "journal" / "0000-transaction_initialized.json"
        if not all(
            os.path.lexists(path)
            for path in (owner_path, intent_path, plan_path, origin_path)
        ):
            return False
        owner, _ = stable_json(owner_path, "runtime bootstrap initialization owner")
        intent, _ = stable_json(intent_path, "runtime bootstrap initialization intent")
        published_plan, _ = stable_json(
            plan_path, "runtime bootstrap initialization plan"
        )
        origin, _ = stable_json(
            origin_path, "runtime bootstrap initialization journal origin"
        )
        if not exact_json_equal(published_plan, dict(plan)):
            return False
        validate_plan_shape(published_plan)
        validate_intent_shape(intent, published_plan)
        if (
            owner.get("schema") != OWNER_SCHEMA
            or owner.get("transaction_id") != published_plan.get("transaction_id")
            or owner.get("root_identity") != node_identity(os.lstat(transaction))
            or set(origin)
            != {
                "schema",
                "index",
                "transaction_id",
                "plan_hash",
                "event",
                "data",
                "previous_entry_hash",
                "entry_hash",
            }
            or origin.get("schema") != JOURNAL_SCHEMA
            or type(origin.get("index")) is not int
            or origin.get("index") != 0
            or origin.get("transaction_id") != published_plan.get("transaction_id")
            or origin.get("plan_hash") != published_plan.get("plan_hash")
            or origin.get("event") != "transaction_initialized"
            or origin.get("previous_entry_hash") is not None
        ):
            return False
        data = origin.get("data")
        if not isinstance(data, Mapping):
            return False
        validate_journal_data("transaction_initialized", data, published_plan)
        unhashed = dict(origin)
        claimed_hash = unhashed.pop("entry_hash", None)
        if canonical_hash(unhashed) != claimed_hash:
            return False
        publications = data["publication_identities"]
        for key, path in (
            ("owner", owner_path),
            ("intent", intent_path),
            ("plan", plan_path),
        ):
            if publications.get(key) != node_identity(os.lstat(path)):
                return False
        return True
    except (BootstrapError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def make_directory_create_only(path: pathlib.Path, label: str) -> None:
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise BootstrapError(f"{label} appeared concurrently") from exc
    if is_reparse(path) or not path.is_dir():
        raise BootstrapError(f"{label} is not an owned directory")


def build_probe_stage(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    payloads: Mapping[str, bytes],
    hook: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    transaction = roots.transaction_dir
    stage_root = transaction / "stage"
    stage_root.mkdir(exist_ok=True)
    if is_reparse(stage_root):
        raise BootstrapError("stage root is reparse-backed")
    stage = stage_root / "probe"
    expected_content = {
        "exists": True,
        "entries": [
            {
                "kind": "file",
                "relative": name,
                "length": plan["inputs"]["release_artifacts"][name]["length"],
                "sha256": plan["inputs"]["release_artifacts"][name]["sha256"],
            }
            for name in sorted(ARTIFACT_NAMES, key=str.casefold)
        ],
    }
    event = journal_event(transaction, "probe_staged")
    if event is not None:
        if not os.path.lexists(stage):
            identities = dict(event["data"]["identity"])
            live_probe = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
            if not tree_matches_bound_content_identity(live_probe, expected_content, identities):
                raise BootstrapError("journaled Probe stage disappeared before an owned install")
            return {"content": expected_content, "identity": identities}
        if not tree_matches_bound_content_identity(stage, expected_content, event["data"]["identity"]):
            raise BootstrapError("Probe stage content was externally changed")
        identities = identity_tree(stage)
        if not exact_json_equal(event.get("data", {}).get("identity"), identities):
            raise BootstrapError("Probe stage identity was externally replaced")
    else:
        owner, _ = stable_json(transaction / "owner.json", "runtime bootstrap owner")
        if (
            not os.path.lexists(stage)
            or node_identity(os.lstat(stage)) != owner.get("probe_stage_identity")
            or is_reparse(stage)
        ):
            raise BootstrapError("Probe stage root lost its pre-published custody")
        allowed_names = set(ARTIFACT_NAMES) | {name + ".new" for name in ARTIFACT_NAMES}
        if any(
            path.name not in allowed_names or path.parent != stage
            for path in _logical_tree_paths(stage)
        ):
            raise BootstrapError("Probe stage contains an unexpected partial node")
        for name in ARTIFACT_NAMES:
            target = stage / name
            write_bytes_create_only(target, payloads[name], f"staged Probe artifact {name}")
            source_state = plan["inputs"]["release_artifacts"][name]
            os.utime(
                target,
                ns=(int(source_state["last_write_ns"]), int(source_state["last_write_ns"])),
            )
        if not tree_matches_content(stage, expected_content):
            raise BootstrapError("built Probe stage failed verification")
        identities = identity_tree(stage)
        checkpoint(args, "after_probe_stage_physical", hook)
    if event is None:
        append_journal(
            transaction,
            "probe_staged",
            {"content_hash": canonical_hash(expected_content), "identity": identities},
        )
        checkpoint(args, "after_probe_stage_journal", hook)
    return {"content": expected_content, "identity": identities}


def bridge_expected_content(plan: Mapping[str, Any]) -> dict[str, Any]:
    desired = plan["desired"]
    entries: list[dict[str, Any]] = []
    directories = [
        *(f"commands/{name}" for name in COMMAND_QUEUES),
        *(f"validation-controls/{name}" for name in CONTROL_QUEUES),
        "evidence",
    ]
    # Full relative-path case-insensitive ordering keeps parents before descendants.
    directories.extend(("commands", "validation-controls"))
    for relative in sorted(set(directories), key=str.casefold):
        entries.append({"kind": "directory", "relative": relative})
    files = {
        "action-policy.json": document_bytes(desired["action_policy"]),
        "v27-runtime-bootstrap.json": document_bytes(desired["bootstrap"]),
        "validation-control-policy.json": document_bytes(desired["validation_control_policy"]),
    }
    for relative in sorted(files, key=str.casefold):
        payload = files[relative]
        entries.append(
            {
                "kind": "file",
                "relative": relative,
                "length": len(payload),
                "sha256": sha256_bytes(payload),
            }
        )
    entries.sort(key=lambda item: item["relative"].casefold())
    return {"exists": True, "entries": entries}


def build_bridge_stage(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    transaction = roots.transaction_dir
    stage_root = transaction / "stage"
    stage_root.mkdir(exist_ok=True)
    stage = stage_root / "bridge"
    expected_content = bridge_expected_content(plan)
    event = journal_event(transaction, "bridge_staged")
    if event is not None:
        if not os.path.lexists(stage):
            identities = dict(event["data"]["identity"])
            live_bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
            if not tree_matches_bound_content_identity(live_bridge, expected_content, identities):
                raise BootstrapError("journaled Bridge stage disappeared before an owned install")
            return {"content": expected_content, "identity": identities}
        if not tree_matches_bound_content_identity(stage, expected_content, event["data"]["identity"]):
            raise BootstrapError("Bridge stage content was externally changed")
        identities = identity_tree(stage)
        if not exact_json_equal(event.get("data", {}).get("identity"), identities):
            raise BootstrapError("Bridge stage identity was externally replaced")
    else:
        owner, _ = stable_json(transaction / "owner.json", "runtime bootstrap owner")
        if (
            not os.path.lexists(stage)
            or node_identity(os.lstat(stage)) != owner.get("bridge_stage_identity")
            or is_reparse(stage)
        ):
            raise BootstrapError("Bridge stage root lost its pre-published custody")
        expected_directories = {
            item["relative"]
            for item in expected_content["entries"]
            if item["kind"] == "directory"
        }
        expected_files = {
            item["relative"]
            for item in expected_content["entries"]
            if item["kind"] == "file"
        }
        for path in _logical_tree_paths(stage):
            relative = path.relative_to(stage).as_posix()
            metadata = _lstat(path)
            if is_reparse_metadata(metadata):
                raise BootstrapError("Bridge stage contains an unsafe partial node")
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in expected_directories:
                    raise BootstrapError("Bridge stage contains an unexpected directory")
            elif stat.S_ISREG(metadata.st_mode):
                if relative not in expected_files and not (
                    relative.endswith(".new") and relative[:-4] in expected_files
                ):
                    raise BootstrapError("Bridge stage contains an unexpected partial file")
            else:
                raise BootstrapError("Bridge stage contains an unsafe partial node")
        for queue in COMMAND_QUEUES:
            (stage / "commands" / queue).mkdir(parents=True, exist_ok=True)
        for queue in CONTROL_QUEUES:
            (stage / "validation-controls" / queue).mkdir(parents=True, exist_ok=True)
        (stage / "evidence").mkdir(exist_ok=True)
        write_json_create_only(
            stage / "action-policy.json",
            plan["desired"]["action_policy"],
            "staged action policy",
        )
        write_json_create_only(
            stage / "validation-control-policy.json",
            plan["desired"]["validation_control_policy"],
            "staged validation-control policy",
        )
        write_json_create_only(
            stage / "v27-runtime-bootstrap.json",
            plan["desired"]["bootstrap"],
            "staged runtime bootstrap",
        )
        if not tree_matches_content(stage, expected_content):
            raise BootstrapError("built Bridge stage failed exact-tree verification")
        identities = identity_tree(stage)
        checkpoint(args, "after_bridge_stage_physical", hook)
    if event is None:
        append_journal(
            transaction,
            "bridge_staged",
            {"content_hash": canonical_hash(expected_content), "identity": identities},
        )
        checkpoint(args, "after_bridge_stage_journal", hook)
    return {"content": expected_content, "identity": identities}


def ensure_external_anchors(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
) -> None:
    require_processes_stopped(args, roots)
    validate_manifest_anchor(args, roots, plan)
    if physical_commit_started(roots, plan):
        frozen_probe_payloads(roots, plan)
    else:
        validate_release_anchors(args, roots, plan)
        initial_paths = {
            "probe": roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts),
            "old_bridge": roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts),
            "archive_root": roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts),
        }
        for key, path in initial_paths.items():
            record = plan.get("prestate", {}).get(key, {})
            if not optional_tree_state(path, record.get("snapshot", {}), record.get("identity", {})):
                raise BootstrapError("initial ONI runtime prestate identity/content changed")
    intent, current_plan = load_published_transaction(roots)
    if not exact_json_equal(current_plan, dict(plan)) or intent.get("plan_hash") != plan.get("plan_hash"):
        raise BootstrapError("runtime bootstrap transaction publications changed")


def rename_create_only(source: pathlib.Path, target: pathlib.Path, label: str) -> None:
    if not os.path.lexists(source):
        raise BootstrapError(f"{label} source is missing")
    if os.path.lexists(target):
        raise BootstrapError(f"{label} target collision")
    try:
        os.rename(source, target)
    except OSError as exc:
        raise BootstrapError(f"{label} atomic rename failed") from exc


def rename_bound_tree(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    expected_identity: Mapping[str, Any],
    label: str,
    expected_snapshot: Optional[Mapping[str, Any]] = None,
    expected_content: Optional[Mapping[str, Any]] = None,
) -> None:
    """Rename one exact tree and immediately prove same-inode custody."""

    source = normalized(source)
    target = normalized(target)
    if (expected_snapshot is None) == (expected_content is None):
        raise BootstrapError("bound rename requires exactly one content contract")
    source_parent = os.lstat(source.parent)
    target_parent = os.lstat(target.parent)
    if (
        is_reparse_metadata(source_parent)
        or is_reparse_metadata(target_parent)
        or not stat.S_ISDIR(source_parent.st_mode)
        or not stat.S_ISDIR(target_parent.st_mode)
    ):
        raise BootstrapError(f"{label} parent is unsafe")
    if expected_snapshot is not None:
        valid_source = optional_tree_state(source, expected_snapshot, expected_identity)
    else:
        valid_source = tree_matches_bound_content_identity(
            source, expected_content or {}, expected_identity
        )
    if not valid_source or os.path.lexists(target):
        raise BootstrapError(f"{label} source custody or target absence is invalid")
    source_root_identity = expected_identity.get("root_identity")
    if not _valid_identity_list(source_root_identity):
        raise BootstrapError(f"{label} source root identity is invalid")
    try:
        os.rename(source, target)
    except OSError as exc:
        raise BootstrapError(
            f"{label} atomic rename failed",
            diagnostic=rename_failure_diagnostic(
                exc,
                source,
                target,
                label,
            ),
        ) from exc
    if os.path.lexists(source):
        raise BootstrapError(f"{label} source name survived atomic rename")
    if expected_snapshot is not None:
        valid_target = optional_tree_state(target, expected_snapshot, expected_identity)
    else:
        valid_target = tree_matches_bound_content_identity(
            target, expected_content or {}, expected_identity
        )
    if (
        not valid_target
        or node_identity(os.lstat(target)) != source_root_identity
        or node_identity(os.lstat(source.parent)) != node_identity(source_parent)
        or node_identity(os.lstat(target.parent)) != node_identity(target_parent)
    ):
        raise BootstrapError(f"{label} post-rename custody is outcome_unknown")


def archive_remainder_matches(
    root: pathlib.Path,
    plan: Mapping[str, Any],
    *,
    allow_target: bool,
) -> bool:
    expected_snapshot = plan["prestate"]["archive_root"]["snapshot"]
    expected_identity = plan["prestate"]["archive_root"]["identity"]
    target_relative = plan["desired"].get("archive_relative")
    target_name = pathlib.PurePosixPath(str(target_relative)).name if target_relative else None
    try:
        actual_snapshot, actual_identity = capture_bound_tree(root)
    except BootstrapError:
        return False
    if expected_snapshot.get("exists") is not True:
        if not allow_target:
            return actual_snapshot.get("exists") is False
        # An owned newly-created root may contain only the intended archive.
        permitted_prefix = (target_name + "/") if target_name else ""
        for item in actual_snapshot.get("entries", []):
            relative = str(item.get("relative") or "")
            if relative != target_name and not relative.startswith(permitted_prefix):
                return False
        return True
    if actual_snapshot.get("exists") is not True:
        return False
    if actual_identity.get("root_identity") != expected_identity.get("root_identity"):
        return False
    filtered_snapshot = [
        item
        for item in actual_snapshot.get("entries", [])
        if not allow_target
        or (item.get("relative") != target_name and not str(item.get("relative") or "").startswith(target_name + "/"))
    ]
    filtered_identity = [
        item
        for item in actual_identity.get("entries", [])
        if not allow_target
        or (item.get("relative") != target_name and not str(item.get("relative") or "").startswith(target_name + "/"))
    ]
    expected_snapshot_entries = list(expected_snapshot.get("entries", []))
    expected_identity_entries = list(expected_identity.get("entries", []))
    return exact_json_equal(filtered_snapshot, expected_snapshot_entries) and exact_json_equal(
        filtered_identity, expected_identity_entries
    )


def ensure_archive_root(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> None:
    transaction = roots.transaction_dir
    old_exists = plan["prestate"]["old_bridge"]["snapshot"].get("exists") is True
    archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    if not old_exists:
        if not archive_remainder_matches(archive_root, plan, allow_target=False):
            raise BootstrapError("archive root changed although no old Bridge requires an archive")
        return
    event = journal_event(transaction, "archive_root_ready")
    pre_exists = plan["prestate"]["archive_root"]["snapshot"].get("exists") is True
    if pre_exists:
        if not archive_remainder_matches(archive_root, plan, allow_target=True):
            raise BootstrapError("preexisting archive root or its entries changed")
        identity = plan["prestate"]["archive_root"]["identity"]["root_identity"]
        data = {"created": False, "root_identity": identity}
    else:
        if event is not None:
            event_data = event.get("data", {})
            expected_identity = event_data.get("root_identity")
            if (
                event_data.get("created") is not True
                or not isinstance(expected_identity, list)
                or node_identity(os.lstat(archive_root)) != expected_identity
                or not archive_remainder_matches(archive_root, plan, allow_target=True)
            ):
                raise BootstrapError("journaled archive root custody changed")
            return
        prepared_event = journal_event(transaction, "archive_root_prepared")
        prepared_stage = transaction / "stage" / "archive-root"
        if prepared_event is None:
            owner, _ = stable_json(transaction / "owner.json", "runtime bootstrap owner")
            if (
                not os.path.lexists(prepared_stage)
                or node_identity(os.lstat(prepared_stage)) != owner.get("archive_stage_identity")
                or not tree_matches_content(prepared_stage, {"exists": True, "entries": []})
            ):
                raise BootstrapError("archive-root preparation lost its pre-published custody")
            prepared_data = {"identity": identity_tree(prepared_stage)}
            checkpoint(args, "after_archive_root_stage_physical", hook)
            append_journal(transaction, "archive_root_prepared", prepared_data)
            checkpoint(args, "after_archive_root_stage_journal", hook)
            prepared_event = journal_event(transaction, "archive_root_prepared")
        prepared_identity = prepared_event.get("data", {}).get("identity")
        if not isinstance(prepared_identity, Mapping):
            raise BootstrapError("archive-root prepared custody is invalid")
        at_stage = tree_matches_bound_content_identity(
            prepared_stage, {"exists": True, "entries": []}, prepared_identity
        )
        at_live = tree_matches_bound_content_identity(
            archive_root, {"exists": True, "entries": []}, prepared_identity
        )
        if at_stage and not os.path.lexists(archive_root):
            checkpoint(args, "before_archive_root_physical", hook)
            ensure_external_anchors(args, roots, plan)
            rename_bound_tree(
                prepared_stage,
                archive_root,
                expected_identity=prepared_identity,
                expected_content={"exists": True, "entries": []},
                label="archive-root install",
            )
            checkpoint(args, "after_archive_root_physical", hook)
            at_live = tree_matches_bound_content_identity(
                archive_root, {"exists": True, "entries": []}, prepared_identity
            )
        elif not at_live:
            if os.path.lexists(archive_root):
                raise BootstrapError("archive-root target collision or name swap")
            raise BootstrapError("archive-root preparation is in an ambiguous state")
        if os.path.lexists(prepared_stage):
            raise BootstrapError("archive root exists at both staged/live names")
        identity = prepared_identity["root_identity"]
        data = {"created": True, "root_identity": identity}
        if event is not None and not exact_json_equal(event.get("data"), data):
            raise BootstrapError("archive-root recovery identity mismatch")
        if not archive_remainder_matches(archive_root, plan, allow_target=True):
            raise BootstrapError("created archive root contains an external entry")
    if event is None:
        append_journal(transaction, "archive_root_ready", data)
        checkpoint(args, "after_archive_root_journal", hook)


def ensure_old_bridge_archived(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> None:
    transaction = roots.transaction_dir
    pre = plan["prestate"]["old_bridge"]
    live = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    archive_relative = plan["desired"].get("archive_relative")
    event = journal_event(transaction, "old_bridge_archived")
    if pre["snapshot"].get("exists") is not True:
        data = {"present": False, "archive_relative": None}
        if event is None:
            append_journal(transaction, "old_bridge_archived", data)
            checkpoint(args, "after_old_bridge_journal", hook)
        elif not exact_json_equal(event.get("data"), data):
            raise BootstrapError("absent old-Bridge journal is invalid")
        return
    archive = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(archive_relative).parts)
    old_at_live = optional_tree_state(live, pre["snapshot"], pre["identity"])
    old_at_archive = optional_tree_state(archive, pre["snapshot"], pre["identity"])
    if old_at_live and not os.path.lexists(archive):
        checkpoint(args, "before_old_bridge_physical", hook)
        ensure_external_anchors(args, roots, plan)
        if not optional_tree_state(live, pre["snapshot"], pre["identity"]):
            raise BootstrapError("old Bridge changed at its commit boundary")
        rename_bound_tree(
            live,
            archive,
            expected_identity=pre["identity"],
            expected_snapshot=pre["snapshot"],
            label="old Bridge archive",
        )
        checkpoint(args, "after_old_bridge_physical", hook)
        old_at_archive = optional_tree_state(archive, pre["snapshot"], pre["identity"])
    elif not old_at_archive:
        if os.path.lexists(archive):
            raise BootstrapError("archive target collision or replacement")
        raise BootstrapError("old Bridge is in an ambiguous transaction state")
    if os.path.lexists(live) and not tree_matches_content(live, bridge_expected_content(plan)):
        raise BootstrapError("Bridge live name was externally replaced after archive")
    if not old_at_archive or not archive_remainder_matches(
        roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts), plan, allow_target=True
    ):
        raise BootstrapError("archived old Bridge or archive remainder is invalid")
    archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    data = {
        "present": True,
        "archive_relative": archive_relative,
        "archive_identity": pre["identity"],
        "archive_root_last_write_ns": int(os.lstat(archive_root).st_mtime_ns),
    }
    if event is None:
        append_journal(transaction, "old_bridge_archived", data)
        checkpoint(args, "after_old_bridge_journal", hook)
    elif not exact_json_equal(event.get("data"), data):
        raise BootstrapError("archived old Bridge journal/root mtime changed")


def ensure_old_probe_recovered(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> None:
    transaction = roots.transaction_dir
    pre = plan["prestate"]["probe"]
    live = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
    recovery_root = transaction / "recovery"
    recovery_root.mkdir(exist_ok=True)
    if is_reparse(recovery_root):
        raise BootstrapError("Probe recovery root is unsafe")
    recovery = recovery_root / "old-probe"
    event = journal_event(transaction, "old_probe_recovered")
    if pre["snapshot"].get("exists") is not True:
        data = {"present": False, "recovery_relative": None}
        if os.path.lexists(recovery):
            raise BootstrapError("unexpected old-Probe recovery appeared")
        if event is None:
            append_journal(transaction, "old_probe_recovered", data)
            checkpoint(args, "after_old_probe_journal", hook)
        elif not exact_json_equal(event.get("data"), data):
            raise BootstrapError("absent old-Probe journal is invalid")
        return
    old_at_live = optional_tree_state(live, pre["snapshot"], pre["identity"])
    old_at_recovery = optional_tree_state(recovery, pre["snapshot"], pre["identity"])
    if old_at_live and not os.path.lexists(recovery):
        checkpoint(args, "before_old_probe_physical", hook)
        ensure_external_anchors(args, roots, plan)
        if not optional_tree_state(live, pre["snapshot"], pre["identity"]):
            raise BootstrapError("old Probe changed at its commit boundary")
        rename_bound_tree(
            live,
            recovery,
            expected_identity=pre["identity"],
            expected_snapshot=pre["snapshot"],
            label="old Probe recovery",
        )
        checkpoint(args, "after_old_probe_physical", hook)
        old_at_recovery = optional_tree_state(recovery, pre["snapshot"], pre["identity"])
    elif not old_at_recovery:
        if os.path.lexists(recovery):
            raise BootstrapError("old Probe recovery target was externally replaced")
        raise BootstrapError("old Probe is in an ambiguous transaction state")
    if not old_at_recovery:
        raise BootstrapError("old Probe recovery verification failed")
    data = {
        "present": True,
        "recovery_relative": "recovery/old-probe",
        "recovery_identity": pre["identity"],
    }
    if event is None:
        append_journal(transaction, "old_probe_recovered", data)
        checkpoint(args, "after_old_probe_journal", hook)
    elif not exact_json_equal(event.get("data"), data):
        raise BootstrapError("old Probe recovery journal is invalid")


def ensure_new_tree_installed(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    staged: Mapping[str, Any],
    *,
    kind: str,
    hook: Optional[Callable[[str], None]],
) -> None:
    transaction = roots.transaction_dir
    if kind == "probe":
        stage = transaction / "stage" / "probe"
        live = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
        event_name = "new_probe_installed"
    elif kind == "bridge":
        stage = transaction / "stage" / "bridge"
        live = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
        event_name = "new_bridge_installed"
    else:
        raise BootstrapError("unknown install tree kind")
    event = journal_event(transaction, event_name)
    at_stage = tree_matches_bound_content_identity(stage, staged["content"], staged["identity"])
    at_live = tree_matches_bound_content_identity(live, staged["content"], staged["identity"])
    if at_stage and not os.path.lexists(live):
        checkpoint(args, f"before_new_{kind}_physical", hook)
        ensure_external_anchors(args, roots, plan)
        if not (
            tree_matches_bound_content_identity(stage, staged["content"], staged["identity"])
            and not os.path.lexists(live)
        ):
            raise BootstrapError(f"new {kind} stage changed at commit boundary")
        rename_bound_tree(
            stage,
            live,
            expected_identity=staged["identity"],
            expected_content=staged["content"],
            label=f"new {kind} install",
        )
        checkpoint(args, f"after_new_{kind}_physical", hook)
        at_live = tree_matches_bound_content_identity(
            live, staged["content"], staged["identity"]
        )
    elif not at_live:
        if os.path.lexists(live):
            raise BootstrapError(f"live {kind} name was externally replaced")
        raise BootstrapError(f"new {kind} is in an ambiguous transaction state")
    if os.path.lexists(stage):
        raise BootstrapError(f"new {kind} exists at both stage and live names")
    data = {
        "live_relative": PROBE_RELATIVE.as_posix() if kind == "probe" else BRIDGE_RELATIVE.as_posix(),
        "content_hash": canonical_hash(staged["content"]),
        "identity": staged["identity"],
    }
    if event is None:
        append_journal(transaction, event_name, data)
        checkpoint(args, f"after_new_{kind}_journal", hook)
    elif not exact_json_equal(event.get("data"), data):
        raise BootstrapError(f"new {kind} install journal is invalid")


def verify_committed_state(
    roots: Roots,
    plan: Mapping[str, Any],
    probe_stage: Mapping[str, Any],
    bridge_stage: Mapping[str, Any],
) -> dict[str, Any]:
    probe = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
    bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    if not tree_matches_bound_content_identity(probe, probe_stage["content"], probe_stage["identity"]):
        raise BootstrapError("installed Probe does not match its staged custody")
    if not tree_matches_bound_content_identity(bridge, bridge_stage["content"], bridge_stage["identity"]):
        raise BootstrapError("installed Bridge does not match its staged custody")
    bootstrap, _ = stable_json(bridge / "v27-runtime-bootstrap.json", "installed runtime bootstrap")
    if not exact_json_equal(bootstrap, plan["desired"]["bootstrap"]):
        raise BootstrapError("installed runtime bootstrap is not v1-compatible with the plan")
    archive_relative = plan["desired"].get("archive_relative")
    if archive_relative:
        archive = roots.oni_root / pathlib.Path(*pathlib.PurePosixPath(archive_relative).parts)
        old = plan["prestate"]["old_bridge"]
        if not optional_tree_state(archive, old["snapshot"], old["identity"]):
            raise BootstrapError("old Bridge archive lost its captured custody")
        archive_event = journal_event(roots.transaction_dir, "old_bridge_archived")
        archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
        if (
            archive_event is None
            or archive_event.get("data", {}).get("archive_root_last_write_ns")
            != int(os.lstat(archive_root).st_mtime_ns)
            or not archive_remainder_matches(archive_root, plan, allow_target=True)
        ):
            raise BootstrapError("archive root custody/mtime changed after commit")
    else:
        archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
        if not archive_remainder_matches(archive_root, plan, allow_target=False):
            raise BootstrapError("archive root changed for an old-Bridge-absent transaction")
    old_probe = plan["prestate"]["probe"]
    recovery = roots.transaction_dir / "recovery" / "old-probe"
    if old_probe["snapshot"].get("exists") is True:
        if not optional_tree_state(recovery, old_probe["snapshot"], old_probe["identity"]):
            raise BootstrapError("old Probe recovery lost its captured custody")
    elif os.path.lexists(recovery):
        raise BootstrapError("unexpected old Probe recovery exists")
    return {
        "probe_tree_content_hash": canonical_hash(probe_stage["content"]),
        "bridge_tree_content_hash": canonical_hash(bridge_stage["content"]),
        "archive_relative": archive_relative,
        "old_probe_recovery": "recovery/old-probe"
        if old_probe["snapshot"].get("exists") is True
        else None,
    }


def _advisory_lock(handle: Any, *, acquire: bool) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError as exc:
            raise BootstrapError("shared environment lock is held by another transaction") from exc
    else:
        import fcntl

        try:
            fcntl.flock(
                handle.fileno(),
                (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN,
            )
        except OSError as exc:
            raise BootstrapError("shared environment lock is held by another transaction") from exc


def _seal_contract_document(value: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    document = dict(value)
    document[hash_field] = canonical_hash(document)
    return document


def _lock_attempt_contract(
    roots: Roots,
    plan: Mapping[str, Any],
    epoch: int,
    purpose: str,
) -> tuple[dict[str, Any], bytes]:
    if purpose not in {"install", "replay", "abort"} or type(epoch) is not int or epoch < 0:
        raise BootstrapError("environment-lock attempt purpose/epoch is invalid")
    token = canonical_hash(
        {
            "schema": "oni.live-runtime-bootstrap-environment-lock-token.v1",
            "run_id": roots.run_id,
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "epoch": epoch,
            "purpose": purpose,
        }
    )
    intent = _seal_contract_document(
        {
            "schema": SHARED_LOCK_ATTEMPT_SCHEMA,
            "run_id": roots.run_id,
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "epoch": epoch,
            "purpose": purpose,
            "owner_token": token,
            "lock_path_id_lower": path_id(roots.environment_dir / ".finalize.lock"),
        },
        "intent_hash",
    )
    payload = document_bytes(
        {
            "schema": SHARED_LOCK_SCHEMA,
            "run_id": roots.run_id,
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "epoch": epoch,
            "purpose": purpose,
            "owner_token": token,
            "intent_hash": intent["intent_hash"],
        }
    )
    return intent, payload


def _lock_attempt_owner_contract(
    plan: Mapping[str, Any], epoch: int, purpose: str, identity: Sequence[int]
) -> dict[str, Any]:
    return _seal_contract_document(
        {
            "schema": "oni.live-runtime-bootstrap-environment-lock-attempt-owner.v1",
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "epoch": epoch,
            "purpose": purpose,
            "directory_identity": list(identity),
        },
        "owner_hash",
    )


def _lock_attempt_root_claim_contract(
    plan: Mapping[str, Any], epoch: int, purpose: str, identity: Sequence[int]
) -> dict[str, Any]:
    """Bind an attempt directory from its already-owned parent namespace.

    The parent claim is published before any child publication.  An attempt
    directory without this final claim is deliberately an outcome-unknown
    latch; byte-identical child files are never enough to adopt it.
    """

    return _seal_contract_document(
        {
            "schema": SHARED_LOCK_ATTEMPT_ROOT_CLAIM_SCHEMA,
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "epoch": epoch,
            "purpose": purpose,
            "directory_identity": list(identity),
        },
        "claim_hash",
    )


def _lock_claim_contract(
    intent: Mapping[str, Any], payload: bytes, identity: Sequence[int]
) -> dict[str, Any]:
    return _seal_contract_document(
        {
            "schema": SHARED_LOCK_CLAIM_SCHEMA,
            "transaction_id": intent["transaction_id"],
            "plan_hash": intent["plan_hash"],
            "epoch": intent["epoch"],
            "purpose": intent["purpose"],
            "intent_hash": intent["intent_hash"],
            "payload_sha256": sha256_bytes(payload),
            "lock_identity": list(identity),
        },
        "claim_hash",
    )


def _lock_release_contract(claim: Mapping[str, Any]) -> dict[str, Any]:
    return _seal_contract_document(
        {
            "schema": SHARED_LOCK_RELEASE_SCHEMA,
            "transaction_id": claim["transaction_id"],
            "plan_hash": claim["plan_hash"],
            "epoch": claim["epoch"],
            "purpose": claim["purpose"],
            "intent_hash": claim["intent_hash"],
            "claim_hash": claim["claim_hash"],
            "released_identity": claim["lock_identity"],
            "payload_sha256": claim["payload_sha256"],
        },
        "release_hash",
    )


def _validate_exact_publication(
    path: pathlib.Path,
    expected: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    value, _ = stable_json(path, label)
    if not exact_json_equal(value, dict(expected)):
        raise BootstrapError(f"{label} is not its exact deterministic contract")
    return value


def _validate_partial_publication(
    path: pathlib.Path,
    expected_payload: bytes,
    label: str,
) -> None:
    state = stable_file(path, max_bytes=max(MAX_JSON_BYTES, len(expected_payload)), collect=True)
    actual = state.get("bytes")
    if (
        not isinstance(actual, bytes)
        or len(actual) > len(expected_payload)
        or expected_payload[: len(actual)] != actual
    ):
        raise BootstrapError(f"{label} partial publication is not an owned exact prefix")


def rename_bound_file(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    expected_identity: Sequence[int],
    expected_payload: bytes,
    label: str,
) -> None:
    """Move one single-link file and prove same-inode custody immediately."""

    source = normalized(source)
    target = normalized(target)
    if not _valid_identity_list(list(expected_identity)):
        raise BootstrapError(f"{label} expected identity is invalid")
    source_parent = os.lstat(source.parent)
    target_parent = os.lstat(target.parent)
    before = stable_file(source, max_bytes=max(MAX_JSON_BYTES, len(expected_payload)), collect=True)
    if (
        before.get("identity") != list(expected_identity)
        or before.get("bytes") != expected_payload
        or os.path.lexists(target)
        or is_reparse_metadata(source_parent)
        or is_reparse_metadata(target_parent)
        or not stat.S_ISDIR(source_parent.st_mode)
        or not stat.S_ISDIR(target_parent.st_mode)
    ):
        raise BootstrapError(f"{label} source/target custody is invalid")
    try:
        os.rename(source, target)
    except OSError as exc:
        raise BootstrapError(
            f"{label} atomic rename failed",
            diagnostic=rename_failure_diagnostic(
                exc,
                source,
                target,
                label,
            ),
        ) from exc
    after = stable_file(target, max_bytes=max(MAX_JSON_BYTES, len(expected_payload)), collect=True)
    if (
        os.path.lexists(source)
        or after.get("identity") != list(expected_identity)
        or after.get("bytes") != expected_payload
        or node_identity(os.lstat(source.parent)) != node_identity(source_parent)
        or node_identity(os.lstat(target.parent)) != node_identity(target_parent)
    ):
        raise BootstrapError(f"{label} post-rename custody is outcome_unknown")


def _lock_attempt_directories(
    root: pathlib.Path, plan: Mapping[str, Any]
) -> list[pathlib.Path]:
    if is_reparse(root) or not root.is_dir():
        raise BootstrapError("environment-lock attempt root is unsafe")
    nodes = sorted(root.iterdir(), key=lambda item: item.name)
    attempts = [
        item
        for item in nodes
        if re.fullmatch(r"\d{4}-(?:install|replay|abort)", item.name)
    ]
    expected_names: set[str] = set()
    for index, attempt in enumerate(attempts):
        match = re.fullmatch(r"(\d{4})-(install|replay|abort)", attempt.name)
        if (
            match is None
            or int(match.group(1)) != index
            or is_reparse(attempt)
            or not attempt.is_dir()
        ):
            raise BootstrapError("environment-lock attempt pathset is invalid")
        epoch = int(match.group(1))
        purpose = match.group(2)
        claim_path = root / f"{attempt.name}.claim.json"
        expected_names.update({attempt.name, claim_path.name})
        if not os.path.lexists(claim_path):
            if os.path.lexists(claim_path.with_name(claim_path.name + ".new")):
                raise BootstrapError(
                    "environment-lock attempt parent claim is partial; outcome_unknown"
                )
            raise BootstrapError(
                "environment-lock attempt directory has no final parent claim; outcome_unknown"
            )
        expected_claim = _lock_attempt_root_claim_contract(
            plan, epoch, purpose, node_identity(os.lstat(attempt))
        )
        _validate_exact_publication(
            claim_path,
            expected_claim,
            "environment-lock attempt parent claim",
        )
    actual_names = {item.name for item in nodes}
    if actual_names != expected_names:
        raise BootstrapError("environment-lock attempt root contains an unexpected node")
    return attempts


def _load_lock_attempt(
    roots: Roots,
    plan: Mapping[str, Any],
    attempt: pathlib.Path,
) -> dict[str, Any]:
    match = re.fullmatch(r"(\d{4})-(install|replay|abort)", attempt.name)
    if match is None:
        raise BootstrapError("environment-lock attempt name is invalid")
    epoch = int(match.group(1))
    purpose = match.group(2)
    intent_expected, payload = _lock_attempt_contract(roots, plan, epoch, purpose)
    owner_expected = _lock_attempt_owner_contract(
        plan, epoch, purpose, node_identity(os.lstat(attempt))
    )
    allowed_finals = {
        "owner.json",
        "intent.json",
        "lock.stage",
        "claim.json",
        "acquired.json",
        "released.lock",
        "released.json",
    }
    allowed = allowed_finals | {
        name + ".new"
        for name in allowed_finals
        if name not in {"released.lock"}
    }
    names = {item.name for item in attempt.iterdir()}
    if not names <= allowed:
        raise BootstrapError("environment-lock attempt contains an unexpected node")
    if "owner.json" not in names:
        raise BootstrapError(
            "environment-lock attempt exists without final inode-bound ownership; outcome_unknown"
        )
    _validate_exact_publication(attempt / "owner.json", owner_expected, "environment-lock attempt owner")
    partial_names = sorted(name for name in names if name.endswith(".new"))
    if len(partial_names) > 1:
        raise BootstrapError("environment-lock attempt has multiple partial publications")
    expected_publications: dict[str, bytes] = {
        "owner.json": document_bytes(owner_expected),
        "intent.json": document_bytes(intent_expected),
    }
    # owner.json.new cannot coexist with its completed owner publication.
    for partial in partial_names:
        final_name = partial.removesuffix(".new")
        if final_name in names:
            raise BootstrapError("environment-lock attempt retained a completed publication temp")
        if final_name in expected_publications:
            _validate_partial_publication(
                attempt / partial,
                expected_publications[final_name],
                f"environment-lock {final_name}",
            )
    if "intent.json" not in names:
        if names - {"owner.json", "intent.json.new"}:
            raise BootstrapError("environment-lock attempt has nodes without its intent")
        return {
            "epoch": epoch,
            "purpose": purpose,
            "intent": intent_expected,
            "payload": payload,
            "claim": None,
            "released": False,
        }
    _validate_exact_publication(attempt / "intent.json", intent_expected, "environment-lock attempt intent")
    claim: Optional[dict[str, Any]] = None
    if "claim.json" in names:
        claim_value, _ = stable_json(attempt / "claim.json", "environment-lock attempt claim")
        identity = claim_value.get("lock_identity")
        if not _valid_identity_list(identity):
            raise BootstrapError("environment-lock claim identity is invalid")
        expected_claim = _lock_claim_contract(intent_expected, payload, identity)
        if not exact_json_equal(claim_value, expected_claim):
            raise BootstrapError("environment-lock claim is not its exact contract")
        claim = expected_claim
        expected_publications["claim.json"] = document_bytes(expected_claim)
    elif names - {"owner.json", "intent.json"}:
        raise BootstrapError(
            "environment-lock staged inode has no final preclaim; outcome_unknown"
        )
    if "lock.stage.new" in names:
        _validate_partial_publication(
            attempt / "lock.stage.new", payload, "environment-lock staged inode"
        )
    if "claim.json.new" in names:
        raise BootstrapError("partial environment-lock inode claim is not authorization")
    if "acquired.json" in names:
        if claim is None:
            raise BootstrapError("environment-lock acquired publication has no claim")
        acquired = _seal_contract_document(
            {
                "schema": "oni.live-runtime-bootstrap-environment-lock-acquired.v1",
                "transaction_id": claim["transaction_id"],
                "plan_hash": claim["plan_hash"],
                "epoch": claim["epoch"],
                "purpose": claim["purpose"],
                "intent_hash": claim["intent_hash"],
                "claim_hash": claim["claim_hash"],
                "lock_identity": claim["lock_identity"],
                "payload_sha256": claim["payload_sha256"],
            },
            "acquired_hash",
        )
        _validate_exact_publication(attempt / "acquired.json", acquired, "environment-lock acquired evidence")
        expected_publications["acquired.json"] = document_bytes(acquired)
    elif "acquired.json.new" in names:
        if claim is None:
            raise BootstrapError("environment-lock acquired temp has no inode claim")
        acquired = _seal_contract_document(
            {
                "schema": "oni.live-runtime-bootstrap-environment-lock-acquired.v1",
                "transaction_id": claim["transaction_id"],
                "plan_hash": claim["plan_hash"],
                "epoch": claim["epoch"],
                "purpose": claim["purpose"],
                "intent_hash": claim["intent_hash"],
                "claim_hash": claim["claim_hash"],
                "lock_identity": claim["lock_identity"],
                "payload_sha256": claim["payload_sha256"],
            },
            "acquired_hash",
        )
        _validate_partial_publication(
            attempt / "acquired.json.new",
            document_bytes(acquired),
            "environment-lock acquired evidence",
        )
    released = "released.json" in names or "released.lock" in names
    if released:
        if (
            claim is None
            or "acquired.json" not in names
            or names.intersection({"lock.stage", "lock.stage.new"})
        ):
            raise BootstrapError("environment-lock release phase is structurally invalid")
        if "released.lock" not in names or "released.json" not in names:
            # A crash after the physical rename is the sole recoverable missing
            # suffix; the caller publishes released.json after proving custody.
            if claim is None or "released.lock" not in names or "released.json" in names:
                raise BootstrapError("environment-lock release evidence is incomplete/invalid")
        released_state = stable_file(attempt / "released.lock", max_bytes=MAX_JSON_BYTES, collect=True)
        if (
            released_state.get("identity") != claim["lock_identity"]
            or released_state.get("bytes") != payload
        ):
            raise BootstrapError("environment-lock released custody changed")
        if "released.json" in names:
            _validate_exact_publication(
                attempt / "released.json",
                _lock_release_contract(claim),
                "environment-lock release evidence",
            )
        elif "released.json.new" in names:
            _validate_partial_publication(
                attempt / "released.json.new",
                document_bytes(_lock_release_contract(claim)),
                "environment-lock release evidence",
            )
    return {
        "epoch": epoch,
        "purpose": purpose,
        "intent": intent_expected,
        "payload": payload,
        "claim": claim,
        "released": released,
        "release_complete": "released.json" in names,
    }


def _recover_lock_attempt_owner_from_parent_claim(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    attempt: pathlib.Path,
    *,
    requested_purpose: str,
    hook: Optional[Callable[[str], None]],
) -> None:
    """Finish only the child owner authorized by a final parent claim.

    ``_lock_attempt_directories`` has already proved that the create-only
    sibling claim is final, exact, and binds this directory inode inside the
    transaction-owned attempt root.  That durable parent claim is sufficient
    to recover the immediately following owner publication, but nothing else:
    a missing/partial parent claim, a changed directory inode, or any child
    other than the sole exact owner prefix remains outcome-unknown.
    """

    match = re.fullmatch(r"(\d{4})-(install|replay|abort)", attempt.name)
    if match is None:
        raise BootstrapError("environment-lock attempt name is invalid")
    epoch = int(match.group(1))
    purpose = match.group(2)
    owner_path = attempt / "owner.json"
    if os.path.lexists(owner_path):
        return
    resume_purposes = {
        "install": {"install"},
        "replay": {"install", "replay"},
        "abort": {"abort"},
    }
    if requested_purpose not in resume_purposes or purpose not in resume_purposes[
        requested_purpose
    ]:
        raise BootstrapError(
            "unfinished environment-lock epoch must be closed by its owning operation"
        )
    names = {item.name for item in attempt.iterdir()}
    if not names <= {"owner.json.new"}:
        raise BootstrapError(
            "ownerless environment-lock attempt contains an unexpected child; outcome_unknown"
        )
    attempt_identity = node_identity(os.lstat(attempt))
    parent_claim_path = attempt.parent / f"{attempt.name}.claim.json"
    parent_claim = _lock_attempt_root_claim_contract(
        plan, epoch, purpose, attempt_identity
    )
    _validate_exact_publication(
        parent_claim_path,
        parent_claim,
        "environment-lock attempt parent claim",
    )
    owner = _lock_attempt_owner_contract(plan, epoch, purpose, attempt_identity)
    if "owner.json.new" in names:
        _validate_partial_publication(
            attempt / "owner.json.new",
            document_bytes(owner),
            "environment-lock attempt owner",
        )
    write_json_create_only(owner_path, owner, "environment-lock attempt owner")
    checkpoint(args, "after_environment_lock_attempt_dir", hook)


def _prepare_environment_lock_stage(
    args: argparse.Namespace,
    attempt: pathlib.Path,
    intent: Mapping[str, Any],
    payload: bytes,
    hook: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    """Preclaim a staged lock inode before writing any payload bytes."""

    stage = attempt / "lock.stage"
    temporary = attempt / "lock.stage.new"
    claim_path = attempt / "claim.json"
    if os.path.lexists(stage) and os.path.lexists(temporary):
        raise BootstrapError("environment-lock staged inode has two names")
    claim: Optional[dict[str, Any]] = None
    if os.path.lexists(claim_path):
        claim_value, _ = stable_json(claim_path, "environment-lock inode claim")
        identity = claim_value.get("lock_identity")
        if not _valid_identity_list(identity):
            raise BootstrapError("environment-lock inode claim identity is invalid")
        expected = _lock_claim_contract(intent, payload, identity)
        if not exact_json_equal(claim_value, expected):
            raise BootstrapError("environment-lock inode claim contract changed")
        claim = expected
    if claim is None and os.path.lexists(stage):
        raise BootstrapError("environment-lock staged inode exists without a preclaim")
    if claim is None and os.path.lexists(temporary):
        raise BootstrapError(
            "environment-lock staged inode exists without a final preclaim; outcome_unknown"
        )
    if claim is None:
        if os.path.lexists(temporary):
            raise BootstrapError("environment-lock preclaim staging appeared concurrently")
        try:
            handle = temporary.open("x+b")
        except FileExistsError as exc:
            raise BootstrapError("environment-lock staged inode appeared concurrently") from exc
        try:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
                raise BootstrapError("environment-lock staged inode is unsafe")
            claim = _lock_claim_contract(intent, payload, node_identity(metadata))
            # The inode identity and owner token become durable while the
            # descriptor is still open and before any payload byte is written.
            write_json_create_only(claim_path, claim, "environment-lock inode preclaim")
            checkpoint(args, "after_environment_lock_stage_preclaim", hook)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            if node_identity(os.fstat(handle.fileno())) != claim["lock_identity"]:
                raise BootstrapError("environment-lock staged inode changed during write")
            checkpoint(args, "after_environment_lock_stage_fsync", hook)
        finally:
            handle.close()
    assert claim is not None
    if os.path.lexists(temporary):
        state = stable_file(temporary, max_bytes=MAX_JSON_BYTES, collect=True)
        actual = state.get("bytes")
        if (
            state.get("identity") != claim["lock_identity"]
            or not isinstance(actual, bytes)
            or len(actual) > len(payload)
            or payload[: len(actual)] != actual
        ):
            raise BootstrapError("environment-lock staged prefix lost inode custody")
        if len(actual) < len(payload):
            try:
                handle = temporary.open("r+b")
            except OSError as exc:
                raise BootstrapError("environment-lock staged inode could not resume") from exc
            try:
                if node_identity(os.fstat(handle.fileno())) != claim["lock_identity"]:
                    raise BootstrapError("environment-lock resumed descriptor identity changed")
                handle.seek(len(actual))
                handle.write(payload[len(actual) :])
                handle.flush()
                os.fsync(handle.fileno())
                if node_identity(os.fstat(handle.fileno())) != claim["lock_identity"]:
                    raise BootstrapError("environment-lock resumed inode changed during write")
            finally:
                handle.close()
        checkpoint(args, "after_environment_lock_stage_fsync", hook)
        rename_bound_file(
            temporary,
            stage,
            expected_identity=claim["lock_identity"],
            expected_payload=payload,
            label="environment-lock staged inode finalization",
        )
        checkpoint(args, "after_environment_lock_stage_ready", hook)
    state = stable_file(stage, max_bytes=MAX_JSON_BYTES, collect=True)
    if state.get("identity") != claim["lock_identity"] or state.get("bytes") != payload:
        raise BootstrapError("environment-lock staged inode is not its preclaimed payload")
    return claim


@contextlib.contextmanager
def exclusive_environment_lock(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    *,
    purpose: str,
    hook: Optional[Callable[[str], None]],
):
    """Acquire the shared environment namespace through an inode-bound epoch.

    Every lock inode is first frozen under run evidence.  A successful release
    moves that same inode back into immutable evidence and never unlinks it.
    Interrupted epochs are the only live lock files that may be reacquired.
    """

    attempt_root = roots.transaction_dir / "environment-lock-attempts"
    owner, _ = stable_json(roots.transaction_dir / "owner.json", "runtime bootstrap owner")
    if (
        not os.path.lexists(attempt_root)
        or node_identity(os.lstat(attempt_root)) != owner.get("lock_attempt_root_identity")
        or is_reparse(attempt_root)
        or not attempt_root.is_dir()
    ):
        raise BootstrapError("environment-lock attempt root lost ownership")
    attempts = _lock_attempt_directories(attempt_root, plan)
    for prior in attempts[:-1]:
        prior_state = _load_lock_attempt(roots, plan, prior)
        if prior_state.get("release_complete") is not True:
            raise BootstrapError("an earlier environment-lock epoch is not fully released")
    current: Optional[dict[str, Any]] = None
    attempt: Optional[pathlib.Path] = None
    if attempts:
        _recover_lock_attempt_owner_from_parent_claim(
            args,
            roots,
            plan,
            attempts[-1],
            requested_purpose=purpose,
            hook=hook,
        )
        candidate = _load_lock_attempt(roots, plan, attempts[-1])
        resume_purposes = {
            "install": {"install"},
            "replay": {"install", "replay"},
            "abort": {"abort"},
        }
        if (
            not candidate.get("release_complete")
            and candidate.get("purpose") not in resume_purposes[purpose]
        ):
            raise BootstrapError(
                "unfinished environment-lock epoch must be closed by its owning operation"
            )
        if candidate.get("released") and not candidate.get("release_complete"):
            claim = candidate.get("claim")
            assert isinstance(claim, Mapping)
            write_json_create_only(
                attempts[-1] / "released.json",
                _lock_release_contract(claim),
                "environment-lock release evidence",
            )
            checkpoint(args, "after_environment_lock_released", hook)
            candidate = _load_lock_attempt(roots, plan, attempts[-1])
        if not candidate.get("release_complete"):
            current = candidate
            attempt = attempts[-1]
    if current is None:
        epoch = len(attempts)
        if epoch > 9999:
            raise BootstrapError("environment-lock epoch capacity is exhausted")
        attempt = attempt_root / f"{epoch:04d}-{purpose}"
        try:
            attempt.mkdir()
        except FileExistsError as exc:
            raise BootstrapError("environment-lock attempt appeared concurrently") from exc
        checkpoint(
            args,
            "after_environment_lock_attempt_dir_physical_before_parent_claim",
            hook,
        )
        attempt_root_claim = _lock_attempt_root_claim_contract(
            plan, epoch, purpose, node_identity(os.lstat(attempt))
        )
        write_json_create_only(
            attempt_root / f"{attempt.name}.claim.json",
            attempt_root_claim,
            "environment-lock attempt parent claim",
        )
        checkpoint(args, "after_environment_lock_attempt_parent_claim", hook)
        attempt_owner = _lock_attempt_owner_contract(
            plan, epoch, purpose, node_identity(os.lstat(attempt))
        )
        write_json_create_only(
            attempt / "owner.json", attempt_owner, "environment-lock attempt owner"
        )
        checkpoint(args, "after_environment_lock_attempt_dir", hook)
        intent, _ = _lock_attempt_contract(roots, plan, epoch, purpose)
        write_json_create_only(attempt / "intent.json", intent, "environment-lock attempt intent")
        checkpoint(args, "after_environment_lock_intent", hook)
        current = _load_lock_attempt(roots, plan, attempt)
    assert attempt is not None
    intent = current["intent"]
    payload = current["payload"]
    write_json_create_only(attempt / "intent.json", intent, "environment-lock attempt intent")
    stage = attempt / "lock.stage"
    live = roots.environment_dir / ".finalize.lock"
    released = attempt / "released.lock"
    claim = current.get("claim")
    if current.get("release_complete") or os.path.lexists(released):
        raise BootstrapError("released environment-lock epoch cannot be reacquired")
    if claim is None:
        if os.path.lexists(live):
            raise CooperativeLockBusyError(
                "foreign environment lock exists before this epoch's inode claim"
            )
        claim = _prepare_environment_lock_stage(args, attempt, intent, payload, hook)
    else:
        claim = dict(claim)
        if os.path.lexists(stage) or os.path.lexists(attempt / "lock.stage.new"):
            claim = _prepare_environment_lock_stage(args, attempt, intent, payload, hook)
    expected_identity = claim["lock_identity"]
    if os.path.lexists(stage):
        if os.path.lexists(live):
            raise CooperativeLockBusyError(
                "foreign environment lock exists before this epoch's publication"
            )
        checkpoint(args, "before_environment_lock_publish", hook)
        rename_bound_file(
            stage,
            live,
            expected_identity=expected_identity,
            expected_payload=payload,
            label="environment-lock publication",
        )
        checkpoint(args, "after_environment_lock_publish", hook)
    else:
        live_state = stable_file(live, max_bytes=MAX_JSON_BYTES, collect=True)
        if live_state.get("identity") != expected_identity or live_state.get("bytes") != payload:
            raise BootstrapError("environment lock exists without this epoch's inode custody")
    acquired = _seal_contract_document(
        {
            "schema": "oni.live-runtime-bootstrap-environment-lock-acquired.v1",
            "transaction_id": claim["transaction_id"],
            "plan_hash": claim["plan_hash"],
            "epoch": claim["epoch"],
            "purpose": claim["purpose"],
            "intent_hash": claim["intent_hash"],
            "claim_hash": claim["claim_hash"],
            "lock_identity": claim["lock_identity"],
            "payload_sha256": claim["payload_sha256"],
        },
        "acquired_hash",
    )
    write_json_create_only(attempt / "acquired.json", acquired, "environment-lock acquired evidence")
    checkpoint(args, "after_environment_lock_acquired", hook)
    try:
        handle = live.open("r+b")
    except OSError as exc:
        raise BootstrapError("shared environment lock could not be descriptor-opened") from exc
    success = False
    try:
        opened = os.fstat(handle.fileno())
        if (
            node_identity(opened) != expected_identity
            or int(opened.st_nlink) != 1
            or node_identity(os.lstat(live)) != expected_identity
            or handle.read(MAX_JSON_BYTES + 1) != payload
        ):
            raise BootstrapError("shared environment lock ownership/payload changed")
        _advisory_lock(handle, acquire=True)
        try:
            yield
            success = True
        finally:
            if (
                node_identity(os.fstat(handle.fileno())) != expected_identity
                or node_identity(os.lstat(live)) != expected_identity
                or int(os.lstat(live).st_nlink) != 1
            ):
                raise BootstrapError("shared environment lock was name-swapped while held")
            _advisory_lock(handle, acquire=False)
    finally:
        handle.close()
    if not success:
        return
    checkpoint(args, "before_environment_lock_release", hook)
    rename_bound_file(
        live,
        released,
        expected_identity=expected_identity,
        expected_payload=payload,
        label="environment-lock release",
    )
    checkpoint(args, "after_environment_lock_release_physical", hook)
    write_json_create_only(
        attempt / "released.json",
        _lock_release_contract(claim),
        "environment-lock release evidence",
    )
    checkpoint(args, "after_environment_lock_released", hook)


@contextlib.contextmanager
def transaction_lock(transaction: pathlib.Path):
    lock_path = transaction / "transaction.lock"
    owner, _ = stable_json(transaction / "owner.json", "runtime bootstrap owner")
    expected_identity = owner.get("lock_identity")
    try:
        before = os.lstat(lock_path)
        if (
            is_reparse_metadata(before)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_nlink) != 1
            or node_identity(before) != expected_identity
        ):
            raise BootstrapError("runtime bootstrap lock ownership is invalid")
        handle = lock_path.open("r+b")
    except OSError as exc:
        raise BootstrapError("runtime bootstrap lock could not be opened") from exc
    try:
        opened = os.fstat(handle.fileno())
        if (
            node_identity(opened) != expected_identity
            or int(opened.st_nlink) != 1
            or node_identity(os.lstat(lock_path)) != expected_identity
        ):
            raise BootstrapError("runtime bootstrap lock name changed before acquisition")
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if os.fstat(handle.fileno()).st_size != 1 or handle.read(1) != b"\0":
                raise BootstrapError("runtime bootstrap lock token is invalid")
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BootstrapError("another runtime bootstrap transaction is active") from exc
            try:
                yield
            finally:
                if (
                    node_identity(os.fstat(handle.fileno())) != expected_identity
                    or node_identity(os.lstat(lock_path)) != expected_identity
                    or int(os.lstat(lock_path).st_nlink) != 1
                ):
                    raise BootstrapError("runtime bootstrap lock was name-swapped while held")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BootstrapError("another runtime bootstrap transaction is active") from exc
            try:
                yield
            finally:
                if (
                    node_identity(os.fstat(handle.fileno())) != expected_identity
                    or node_identity(os.lstat(lock_path)) != expected_identity
                    or int(os.lstat(lock_path).st_nlink) != 1
                ):
                    raise BootstrapError("runtime bootstrap lock was name-swapped while held")
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _active_environment_lock_binding(
    roots: Roots,
    plan: Mapping[str, Any],
    *,
    purpose: str,
    prefix: str,
) -> dict[str, Any]:
    attempts = _lock_attempt_directories(
        roots.transaction_dir / "environment-lock-attempts", plan
    )
    if not attempts:
        raise BootstrapError("runtime bootstrap candidate has no environment-lock epoch")
    attempt = _load_lock_attempt(roots, plan, attempts[-1])
    claim = attempt.get("claim")
    if (
        attempt.get("purpose") != purpose
        or attempt.get("release_complete")
        or not isinstance(claim, Mapping)
        or not os.path.lexists(attempts[-1] / "acquired.json")
    ):
        raise BootstrapError("runtime bootstrap certificate is outside its active lock epoch")
    release = _lock_release_contract(claim)
    return {
        f"{prefix}_lock_epoch": attempt["epoch"],
        f"{prefix}_lock_claim": dict(claim),
        f"{prefix}_lock_claim_hash": claim["claim_hash"],
        f"{prefix}_lock_release": release,
        f"{prefix}_lock_release_hash": release["release_hash"],
    }


def _active_install_lock_binding(roots: Roots, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _active_environment_lock_binding(
        roots, plan, purpose="install", prefix="install"
    )


def _active_ready_lock_binding(roots: Roots, plan: Mapping[str, Any]) -> dict[str, Any]:
    return _active_environment_lock_binding(
        roots, plan, purpose="replay", prefix="ready"
    )


def _validate_certificate_lock_release(
    roots: Roots,
    plan: Mapping[str, Any],
    certificate: Mapping[str, Any],
    *,
    prefix: str,
    purpose: str,
) -> dict[str, Any]:
    attempts, attempt, claim, expected_release = _certificate_lock_binding(
        roots,
        plan,
        certificate,
        prefix=prefix,
        purpose=purpose,
    )
    epoch = certificate[f"{prefix}_lock_epoch"]
    if attempt.get("release_complete") is not True:
        raise BootstrapError("runtime bootstrap certificate lock release is incomplete")
    release, _ = stable_json(
        attempts[epoch] / "released.json",
        "runtime bootstrap certificate lock release",
    )
    if not exact_json_equal(release, expected_release):
        raise BootstrapError("runtime bootstrap certificate released-lock custody changed")
    return release


def _certificate_lock_binding(
    roots: Roots,
    plan: Mapping[str, Any],
    certificate: Mapping[str, Any],
    *,
    prefix: str,
    purpose: str,
) -> tuple[list[pathlib.Path], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rebuild one certificate's lock claim and predicted release exactly."""

    epoch = certificate.get(f"{prefix}_lock_epoch")
    if type(epoch) is not int or epoch < 0:
        raise BootstrapError("runtime bootstrap certificate lock epoch is invalid")
    attempts = _lock_attempt_directories(
        roots.transaction_dir / "environment-lock-attempts", plan
    )
    if epoch >= len(attempts):
        raise BootstrapError("runtime bootstrap certificate lock epoch is missing")
    attempt = _load_lock_attempt(roots, plan, attempts[epoch])
    claim = attempt.get("claim")
    projected_claim = certificate.get(f"{prefix}_lock_claim")
    projected_release = certificate.get(f"{prefix}_lock_release")
    if (
        attempt.get("purpose") != purpose
        or attempt.get("epoch") != epoch
        or not isinstance(claim, Mapping)
        or not isinstance(projected_claim, Mapping)
        or not isinstance(projected_release, Mapping)
    ):
        raise BootstrapError("runtime bootstrap certificate lock projection is incomplete")
    expected_claim = dict(claim)
    expected_release = _lock_release_contract(expected_claim)
    if (
        certificate.get(f"{prefix}_lock_claim_hash")
        != expected_claim.get("claim_hash")
        or certificate.get(f"{prefix}_lock_release_hash")
        != expected_release.get("release_hash")
        or not exact_json_equal(dict(projected_claim), expected_claim)
        or not exact_json_equal(dict(projected_release), expected_release)
        or canonical_hash(
            {key: value for key, value in projected_claim.items() if key != "claim_hash"}
        )
        != projected_claim.get("claim_hash")
        or canonical_hash(
            {
                key: value
                for key, value in projected_release.items()
                if key != "release_hash"
            }
        )
        != projected_release.get("release_hash")
    ):
        raise BootstrapError("runtime bootstrap certificate lock projection is not canonical")
    return attempts, attempt, expected_claim, expected_release


def _validate_candidate_lock_release(
    roots: Roots, plan: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    return _validate_certificate_lock_release(
        roots,
        plan,
        candidate,
        prefix="install",
        purpose="install",
    )


def _certificate_lock_release_complete(
    roots: Roots,
    plan: Mapping[str, Any],
    certificate: Mapping[str, Any],
    *,
    prefix: str,
    purpose: str,
) -> bool:
    attempts, attempt, _claim, _release = _certificate_lock_binding(
        roots,
        plan,
        certificate,
        prefix=prefix,
        purpose=purpose,
    )
    epoch = certificate[f"{prefix}_lock_epoch"]
    if attempt.get("release_complete"):
        _validate_certificate_lock_release(
            roots,
            plan,
            certificate,
            prefix=prefix,
            purpose=purpose,
        )
        return True
    if epoch != len(attempts) - 1:
        raise BootstrapError("unfinished certificate lock is not the latest epoch")
    return False


def _certificate_lock_phase(
    roots: Roots,
    plan: Mapping[str, Any],
    certificate: Mapping[str, Any],
    *,
    prefix: str,
    purpose: str,
) -> str:
    """Return active, release_record_pending, or released for one bound epoch."""

    attempts, attempt, _claim, _release = _certificate_lock_binding(
        roots,
        plan,
        certificate,
        prefix=prefix,
        purpose=purpose,
    )
    epoch = certificate[f"{prefix}_lock_epoch"]
    if attempt.get("release_complete") is True:
        _validate_certificate_lock_release(
            roots, plan, certificate, prefix=prefix, purpose=purpose
        )
        return "released"
    if epoch != len(attempts) - 1:
        raise BootstrapError("unfinished certificate lock is not the latest epoch")
    return (
        "release_record_pending"
        if attempt.get("released") is True
        else "active"
    )


def result_candidate_document(
    roots: Roots,
    plan: Mapping[str, Any],
    committed: Mapping[str, Any],
) -> dict[str, Any]:
    journal = verify_journal(roots.transaction_dir)
    commit_entries = [entry for entry in journal if entry.get("event") == "commit_verified"]
    if len(commit_entries) != 1:
        raise BootstrapError("runtime bootstrap result has no unique commit journal entry")
    lock_binding = _active_install_lock_binding(roots, plan)
    probe_event = journal_event(roots.transaction_dir, "probe_staged")
    if probe_event is None:
        raise BootstrapError("runtime bootstrap candidate has no Probe custody event")
    installed_probe_artifacts = {
        name: {
            "length": plan["inputs"]["release_artifacts"][name]["length"],
            "sha256": plan["inputs"]["release_artifacts"][name]["sha256"],
        }
        for name in sorted(ARTIFACT_NAMES, key=str.casefold)
    }
    bootstrap_document = plan["desired"]["bootstrap"]
    install_intent, _ = stable_json(
        roots.transaction_dir / "intent.json", "runtime bootstrap intent"
    )
    stage_document, stage_state = _load_initialization_intent(
        roots, install_intent, plan, expected_file_sha256=None
    )
    install_commit_entry = {
        key: value
        for key, value in commit_entries[0].items()
        if key != "entry_hash"
    }
    if canonical_hash(install_commit_entry) != commit_entries[0]["entry_hash"]:
        raise BootstrapError("runtime bootstrap commit projection hash is invalid")
    candidate: dict[str, Any] = {
        "schema": RESULT_CANDIDATE_SCHEMA,
        "status": "candidate_not_ready",
        "classification": "static_probe",
        "run_id": roots.run_id,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "environment_manifest_sha256": plan["inputs"]["environment_manifest"]["sha256"],
        "release_dll_sha256": plan["inputs"]["release_artifacts"][ARTIFACT_NAMES[0]]["sha256"],
        "expected_mod_version": plan["expected_mod_version"],
        "probe_tree_content_hash": committed["probe_tree_content_hash"],
        "installed_probe_artifacts": installed_probe_artifacts,
        "installed_probe_tree_identity": probe_event["data"]["identity"],
        "bridge_tree_content_hash": committed["bridge_tree_content_hash"],
        "bootstrap_document_hash": canonical_hash(bootstrap_document),
        "bootstrap_file_sha256": sha256_bytes(document_bytes(bootstrap_document)),
        "archive_relative": committed["archive_relative"],
        "old_probe_recovery_relative": committed["old_probe_recovery"],
        "install_commit_entry": install_commit_entry,
        "install_commit_entry_hash": commit_entries[0]["entry_hash"],
        "install_stage_sha256": stage_state["sha256"],
        "install_stage_hash": stage_document["stage_hash"],
        "producer": stage_document["producer"],
        **lock_binding,
        "physical_commit_complete": True,
        "game_launched": False,
    }
    candidate["candidate_hash"] = canonical_hash(candidate)
    return candidate


def validate_result_candidate(
    roots: Roots,
    plan: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    require_released_lock: bool,
) -> None:
    commit_event = journal_event(roots.transaction_dir, "commit_verified")
    if commit_event is None:
        raise BootstrapError("runtime bootstrap candidate has no commit verification origin")
    # Reconstruct every field except the active/released lock state.  The lock
    # binding is immutable in the candidate and separately checked below.
    install_intent, _ = stable_json(
        roots.transaction_dir / "intent.json", "runtime bootstrap intent"
    )
    stage_document, stage_state = _load_initialization_intent(
        roots, install_intent, plan, expected_file_sha256=None
    )
    install_commit_entry = {
        key: value for key, value in commit_event.items() if key != "entry_hash"
    }
    if canonical_hash(install_commit_entry) != commit_event["entry_hash"]:
        raise BootstrapError("runtime bootstrap commit projection hash is invalid")
    expected: dict[str, Any] = {
        "schema": RESULT_CANDIDATE_SCHEMA,
        "status": "candidate_not_ready",
        "classification": "static_probe",
        "run_id": roots.run_id,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "environment_manifest_sha256": plan["inputs"]["environment_manifest"]["sha256"],
        "release_dll_sha256": plan["inputs"]["release_artifacts"][ARTIFACT_NAMES[0]]["sha256"],
        "expected_mod_version": plan["expected_mod_version"],
        "probe_tree_content_hash": commit_event["data"]["probe_tree_content_hash"],
        "installed_probe_artifacts": {
            name: {
                "length": plan["inputs"]["release_artifacts"][name]["length"],
                "sha256": plan["inputs"]["release_artifacts"][name]["sha256"],
            }
            for name in sorted(ARTIFACT_NAMES, key=str.casefold)
        },
        "installed_probe_tree_identity": (
            journal_event(roots.transaction_dir, "probe_staged") or {"data": {}}
        )["data"].get("identity"),
        "bridge_tree_content_hash": commit_event["data"]["bridge_tree_content_hash"],
        "bootstrap_document_hash": canonical_hash(plan["desired"]["bootstrap"]),
        "bootstrap_file_sha256": sha256_bytes(
            document_bytes(plan["desired"]["bootstrap"])
        ),
        "archive_relative": commit_event["data"]["archive_relative"],
        "old_probe_recovery_relative": commit_event["data"]["old_probe_recovery"],
        "install_commit_entry": install_commit_entry,
        "install_commit_entry_hash": commit_event["entry_hash"],
        "install_stage_sha256": stage_state["sha256"],
        "install_stage_hash": stage_document["stage_hash"],
        "producer": stage_document["producer"],
        "install_lock_epoch": candidate.get("install_lock_epoch"),
        "install_lock_claim": candidate.get("install_lock_claim"),
        "install_lock_claim_hash": candidate.get("install_lock_claim_hash"),
        "install_lock_release": candidate.get("install_lock_release"),
        "install_lock_release_hash": candidate.get("install_lock_release_hash"),
        "physical_commit_complete": True,
        "game_launched": False,
    }
    expected["candidate_hash"] = canonical_hash(expected)
    if not exact_json_equal(dict(candidate), expected):
        raise BootstrapError("runtime bootstrap result candidate is not exact")
    if require_released_lock:
        _validate_candidate_lock_release(roots, plan, candidate)


def result_document(
    roots: Roots,
    plan: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_state: Mapping[str, Any],
    ready_binding: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    validate_result_candidate(roots, plan, candidate, require_released_lock=True)
    if ready_binding is None:
        ready_binding = _active_ready_lock_binding(roots, plan)
    if (
        set(ready_binding)
        != {
            "ready_lock_epoch",
            "ready_lock_claim",
            "ready_lock_claim_hash",
            "ready_lock_release",
            "ready_lock_release_hash",
        }
        or type(ready_binding.get("ready_lock_epoch")) is not int
        or not isinstance(ready_binding.get("ready_lock_claim"), Mapping)
        or HASH_RE.fullmatch(str(ready_binding.get("ready_lock_claim_hash") or "")) is None
        or not isinstance(ready_binding.get("ready_lock_release"), Mapping)
        or HASH_RE.fullmatch(str(ready_binding.get("ready_lock_release_hash") or "")) is None
    ):
        raise BootstrapError("runtime bootstrap ready-lock binding is invalid")
    result = {
        "schema": RESULT_SCHEMA,
        "status": "installed_ready",
        "classification": "static_probe",
        "run_id": candidate["run_id"],
        "transaction_id": candidate["transaction_id"],
        "plan_hash": candidate["plan_hash"],
        "environment_manifest_sha256": candidate["environment_manifest_sha256"],
        "release_dll_sha256": candidate["release_dll_sha256"],
        "expected_mod_version": candidate["expected_mod_version"],
        "probe_tree_content_hash": candidate["probe_tree_content_hash"],
        "installed_probe_artifacts": candidate["installed_probe_artifacts"],
        "installed_probe_tree_identity": candidate["installed_probe_tree_identity"],
        "bridge_tree_content_hash": candidate["bridge_tree_content_hash"],
        "bootstrap_document_hash": candidate["bootstrap_document_hash"],
        "bootstrap_file_sha256": candidate["bootstrap_file_sha256"],
        "archive_relative": candidate["archive_relative"],
        "old_probe_recovery_relative": candidate["old_probe_recovery_relative"],
        "install_commit_entry": candidate["install_commit_entry"],
        "install_commit_entry_hash": candidate["install_commit_entry_hash"],
        "install_stage_sha256": candidate["install_stage_sha256"],
        "install_stage_hash": candidate["install_stage_hash"],
        "producer": candidate["producer"],
        "install_lock_epoch": candidate["install_lock_epoch"],
        "install_lock_claim": candidate["install_lock_claim"],
        "install_lock_claim_hash": candidate["install_lock_claim_hash"],
        "install_lock_release": candidate["install_lock_release"],
        "install_lock_release_hash": candidate["install_lock_release_hash"],
        **dict(ready_binding),
        "candidate_sha256": candidate_state["sha256"],
        "candidate_hash": candidate["candidate_hash"],
        "physical_commit_complete": True,
        "shared_lock_released": True,
        "game_launched": False,
    }
    result["result_hash"] = canonical_hash(result)
    return result


def validate_result(
    roots: Roots,
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    require_ready_released: bool,
) -> None:
    candidate, candidate_state = stable_json(
        roots.transaction_dir / "result-candidate.json",
        "runtime bootstrap result candidate",
    )
    ready_binding = {
        "ready_lock_epoch": result.get("ready_lock_epoch"),
        "ready_lock_claim": result.get("ready_lock_claim"),
        "ready_lock_claim_hash": result.get("ready_lock_claim_hash"),
        "ready_lock_release": result.get("ready_lock_release"),
        "ready_lock_release_hash": result.get("ready_lock_release_hash"),
    }
    expected = result_document(
        roots, plan, candidate, candidate_state, ready_binding=ready_binding
    )
    if not exact_json_equal(dict(result), expected):
        raise BootstrapError("runtime bootstrap result is not the exact committed-state result")
    if require_ready_released:
        _validate_certificate_lock_release(
            roots,
            plan,
            result,
            prefix="ready",
            purpose="replay",
        )
    else:
        if not exact_json_equal(
            _active_ready_lock_binding(roots, plan), ready_binding
        ):
            raise BootstrapError("runtime bootstrap ready result is outside its publication lock")


def _committed_stage_custody(
    roots: Roots, plan: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    probe_event = journal_event(roots.transaction_dir, "probe_staged")
    bridge_event = journal_event(roots.transaction_dir, "bridge_staged")
    if probe_event is None or bridge_event is None:
        raise BootstrapError("committed runtime has no staged custody journal")
    probe_custody = {
        "content": {
            "exists": True,
            "entries": [
                {
                    "kind": "file",
                    "relative": name,
                    "length": plan["inputs"]["release_artifacts"][name]["length"],
                    "sha256": plan["inputs"]["release_artifacts"][name]["sha256"],
                }
                for name in sorted(ARTIFACT_NAMES, key=str.casefold)
            ],
        },
        "identity": probe_event["data"]["identity"],
    }
    bridge_custody = {
        "content": bridge_expected_content(plan),
        "identity": bridge_event["data"]["identity"],
    }
    return probe_custody, bridge_custody


def _publish_ready_result(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    committed: Mapping[str, Any],
    probe_custody: Mapping[str, Any],
    bridge_custody: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    candidate_path = roots.transaction_dir / "result-candidate.json"
    result_path = roots.transaction_dir / "result.json"
    with exclusive_environment_lock(
        args, roots, plan, purpose="replay", hook=hook
    ):
        validate_manifest_anchor(args, roots, plan)
        candidate, candidate_state = stable_json(
            candidate_path, "runtime bootstrap result candidate"
        )
        validate_result_candidate(roots, plan, candidate, require_released_lock=True)
        if not exact_json_equal(
            verify_committed_state(roots, plan, probe_custody, bridge_custody), committed
        ):
            raise BootstrapError("physical custody changed before ready-result publication")
        result = result_document(roots, plan, candidate, candidate_state)
        write_json_create_only(result_path, result, "runtime bootstrap ready result")
        checkpoint(args, "after_ready_result_publish", hook)
        published, _ = stable_json(result_path, "runtime bootstrap ready result")
        validate_result(
            roots, plan, published, require_ready_released=False
        )
        validate_manifest_anchor(args, roots, plan)
        if not exact_json_equal(
            verify_committed_state(roots, plan, probe_custody, bridge_custody), committed
        ):
            raise BootstrapError("physical custody changed after ready-result publication")
        checkpoint(args, "before_ready_publication_lock_release", hook)
    checkpoint(args, "after_ready_publication_lock_release", hook)
    validate_manifest_anchor(args, roots, plan)
    published, _ = stable_json(result_path, "runtime bootstrap ready result")
    validate_result(roots, plan, published, require_ready_released=True)
    if not exact_json_equal(
        verify_committed_state(roots, plan, probe_custody, bridge_custody), committed
    ):
        raise BootstrapError("physical custody changed after ready-lock release")
    _validate_candidate_lock_release(roots, plan, candidate)
    if os.path.lexists(roots.environment_dir / ".finalize.lock"):
        raise BootstrapError("ready result retained a live shared lock")
    return published


def apply_transaction(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    release_payloads: Mapping[str, bytes],
    hook: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    with transaction_lock(roots.transaction_dir):
        _, published_plan = load_published_transaction(roots)
        if not exact_json_equal(published_plan, dict(plan)):
            raise BootstrapError("published runtime bootstrap plan changed")
        result_path = roots.transaction_dir / "result.json"
        candidate_path = roots.transaction_dir / "result-candidate.json"
        if os.path.lexists(result_path):
            probe_custody, bridge_custody = _committed_stage_custody(roots, plan)
            result_before_lock, _ = stable_json(
                result_path, "runtime bootstrap ready result"
            )
            ready_lock_phase = _certificate_lock_phase(
                roots,
                plan,
                result_before_lock,
                prefix="ready",
                purpose="replay",
            )
            with exclusive_environment_lock(
                args, roots, plan, purpose="replay", hook=hook
            ):
                ensure_external_anchors(args, roots, plan)
                candidate, _ = stable_json(
                    candidate_path, "runtime bootstrap result candidate"
                )
                validate_result_candidate(
                    roots, plan, candidate, require_released_lock=True
                )
                result, _ = stable_json(result_path, "runtime bootstrap result")
                validate_result(
                    roots,
                    plan,
                    result,
                    # If the bound inode has already moved to released custody,
                    # the context manager first fills only its missing release
                    # record and may then use a fresh replay epoch.  The ready
                    # certificate must still be validated against its own
                    # completed epoch rather than the fresh active one.
                    require_ready_released=(
                        ready_lock_phase != "active"
                    ),
                )
                committed = verify_committed_state(
                    roots, plan, probe_custody, bridge_custody
                )
                commit_event = journal_event(roots.transaction_dir, "commit_verified")
                if commit_event is None or not exact_json_equal(
                    commit_event.get("data"), committed
                ):
                    raise BootstrapError("committed runtime state does not match its journal")
                checkpoint(args, "before_completed_lock_release", hook)
            checkpoint(args, "after_environment_lock_release", hook)
            validate_manifest_anchor(args, roots, plan)
            if os.path.lexists(roots.environment_dir / ".finalize.lock"):
                raise BootstrapError("completed replay left a live shared lock")
            result, _ = stable_json(result_path, "runtime bootstrap result")
            validate_result(
                roots, plan, result, require_ready_released=True
            )
            if not exact_json_equal(
                verify_committed_state(roots, plan, probe_custody, bridge_custody), committed
            ):
                raise BootstrapError("completed replay custody changed after lock release")
            return {
                "schema": "oni.live-runtime-bootstrap-replay.v1",
                "status": "already_installed",
                "classification": "static_probe",
                "run_id": roots.run_id,
                "transaction_id": plan["transaction_id"],
                "plan_hash": plan["plan_hash"],
                "result_hash": result["result_hash"],
                "replay": "refused_noop",
                "runtime_write_count": 0,
                "evidence_epoch_written": True,
                "game_launched": False,
            }
        if os.path.lexists(candidate_path):
            probe_custody, bridge_custody = _committed_stage_custody(roots, plan)
            committed_event = journal_event(roots.transaction_dir, "commit_verified")
            if committed_event is None:
                raise BootstrapError("result candidate exists without a commit event")
            committed = dict(committed_event["data"])
            with exclusive_environment_lock(
                args, roots, plan, purpose="replay", hook=hook
            ):
                ensure_external_anchors(args, roots, plan)
                candidate, _ = stable_json(
                    candidate_path, "runtime bootstrap result candidate"
                )
                validate_result_candidate(
                    roots, plan, candidate, require_released_lock=False
                )
                if not exact_json_equal(
                    verify_committed_state(
                        roots, plan, probe_custody, bridge_custody
                    ),
                    committed,
                ):
                    raise BootstrapError("candidate recovery physical custody changed")
                checkpoint(args, "before_candidate_recovery_lock_release", hook)
            checkpoint(args, "after_candidate_recovery_lock_release", hook)
            return _publish_ready_result(
                args,
                roots,
                plan,
                committed,
                probe_custody,
                bridge_custody,
                hook,
            )
        ensure_external_anchors(args, roots, plan)
        probe_stage = build_probe_stage(args, roots, plan, release_payloads, hook)
        ensure_external_anchors(args, roots, plan)
        bridge_stage = build_bridge_stage(args, roots, plan, hook)
        with exclusive_environment_lock(
            args, roots, plan, purpose="install", hook=hook
        ):
            ensure_external_anchors(args, roots, plan)
            ensure_archive_root(args, roots, plan, hook)
            ensure_old_bridge_archived(args, roots, plan, hook)
            ensure_old_probe_recovered(args, roots, plan, hook)
            ensure_new_tree_installed(
                args, roots, plan, probe_stage, kind="probe", hook=hook
            )
            ensure_new_tree_installed(
                args, roots, plan, bridge_stage, kind="bridge", hook=hook
            )
            ensure_external_anchors(args, roots, plan)
            committed = verify_committed_state(roots, plan, probe_stage, bridge_stage)
            append_journal(roots.transaction_dir, "commit_verified", committed)
            checkpoint(args, "after_commit_verified_journal", hook)
            if not exact_json_equal(
                verify_committed_state(roots, plan, probe_stage, bridge_stage), committed
            ):
                raise BootstrapError("physical custody changed after commit journal publication")
            candidate = result_candidate_document(roots, plan, committed)
            write_json_create_only(
                candidate_path, candidate, "runtime bootstrap result candidate"
            )
            checkpoint(args, "after_result_candidate_publish", hook)
            published_candidate, _ = stable_json(
                candidate_path, "published runtime bootstrap result candidate"
            )
            validate_result_candidate(
                roots, plan, published_candidate, require_released_lock=False
            )
            if not exact_json_equal(
                verify_committed_state(roots, plan, probe_stage, bridge_stage), committed
            ):
                raise BootstrapError("physical custody changed after result publication")
            validate_manifest_anchor(args, roots, plan)
        checkpoint(args, "after_environment_lock_release", hook)
        # A final zero-write sandwich closes the lock-release/return gap. A
        # concurrent config/abort append is therefore a refusal, never an
        # installed claim based on a mixed generation.
        validate_manifest_anchor(args, roots, plan)
        published_result = _publish_ready_result(
            args,
            roots,
            plan,
            committed,
            probe_stage,
            bridge_stage,
            hook,
        )
        if not exact_json_equal(
            verify_committed_state(roots, plan, probe_stage, bridge_stage), committed
        ):
            raise BootstrapError("physical custody changed before installed return")
        if any(
            path.name.endswith(".new") for path in _logical_tree_paths(roots.transaction_dir)
        ):
            raise BootstrapError("runtime bootstrap left a temporary publication")
        return published_result


def _safe_relative(value: Any, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        raise BootstrapError(f"{label} relative path is invalid")
    relative = pathlib.PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise BootstrapError(f"{label} relative path is unsafe")
    return relative


def _manifest_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("exists") is not True:
        return {"exists": False, "entries": []}
    return {
        key: value.get(key)
        for key in ("exists", "root_last_write_ns", "entries", "file_count", "total_bytes")
    }


def _boundary_timestamp_ns(value: Any) -> int:
    if not isinstance(value, str):
        raise BootstrapError("pre-boundary save timestamp is invalid")
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})",
        value,
    )
    if match is None:
        raise BootstrapError("pre-boundary save timestamp is invalid")
    offset = "+00:00" if match.group(3) == "Z" else match.group(3)
    try:
        parsed = dt.datetime.fromisoformat(match.group(1) + offset)
    except ValueError as exc:
        raise BootstrapError("pre-boundary save timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise BootstrapError("pre-boundary save timestamp lacks an offset")
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    delta = parsed.astimezone(dt.timezone.utc) - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + int((match.group(2) or "").ljust(9, "0"))
    )


def _verify_pre_boundary_save_delta(
    roots: Roots,
    manifest: Mapping[str, Any],
    paths: Mapping[str, pathlib.Path],
) -> None:
    pre, pre_state = stable_json(paths["pre_boundary"], "pre-boundary report")
    saves = pre.get("saves")
    if (
        not isinstance(saves, Mapping)
        or set(saves) != {"count", "entries"}
        or type(saves.get("count")) is not int
        or not isinstance(saves.get("entries"), list)
        or saves.get("count") != len(saves["entries"])
    ):
        raise BootstrapError("pre-boundary report lacks an exact save inventory")
    expected: dict[str, tuple[int, str, int]] = {}
    entry_keys = {
        "scope",
        "path_id",
        "parent_path_id",
        "file_name_id",
        "extension",
        "length",
        "last_write_utc",
        "sha256",
        "stem_id",
    }
    for entry in saves["entries"]:
        if (
            not isinstance(entry, Mapping)
            or set(entry) != entry_keys
            or entry.get("scope") not in {"save_files", "cloud_save_files"}
            or entry.get("extension") not in {".sav", ".png"}
            or type(entry.get("length")) is not int
            or entry.get("length") < 0
        ):
            raise BootstrapError("pre-boundary save inventory entry is invalid")
        identifier = require_hash(entry.get("path_id"), "pre-boundary save path id")
        require_hash(entry.get("parent_path_id"), "pre-boundary save parent id")
        require_hash(entry.get("file_name_id"), "pre-boundary save filename id")
        require_hash(entry.get("stem_id"), "pre-boundary save stem id")
        digest = require_hash(entry.get("sha256"), "pre-boundary save content hash")
        if identifier in expected:
            raise BootstrapError("pre-boundary save inventory has a duplicate path id")
        expected[identifier] = (
            entry["length"],
            digest,
            _boundary_timestamp_ns(entry["last_write_utc"]),
        )
    bindings = manifest["bindings"]
    for path_key, source_key in (
        ("loader_save", "source_save"),
        ("loader_preview", "source_preview"),
    ):
        source_state = bindings[source_key]["state"]
        identifier = path_id(paths[path_key])
        if identifier in expected:
            raise BootstrapError("bound loader pair already existed in pre-boundary inventory")
        expected[identifier] = (
            source_state["length"],
            source_state["sha256"],
            source_state["last_write_ns"],
        )
    actual: dict[str, tuple[int, str, int]] = {}
    for relative in ("save_files", "cloud_save_files"):
        save_root = roots.oni_root / relative
        if not os.path.lexists(save_root):
            continue
        assert_no_reparse(save_root, roots.oni_root)
        for path in sorted(_logical_tree_paths(save_root), key=lambda item: str(item).casefold()):
            if is_reparse(path):
                raise BootstrapError("current save inventory contains a reparse point")
            metadata = _lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or path.suffix.casefold() not in {".sav", ".png"}:
                continue
            state = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
            identifier = path_id(path)
            if identifier in actual:
                raise BootstrapError("current save inventory produced a duplicate path id")
            actual[identifier] = (
                state["length"],
                state["sha256"],
                state["last_write_ns"],
            )
    if not exact_json_equal(actual, expected):
        raise BootstrapError(
            "current save inventory differs from pre-boundary plus the one bound loader pair"
        )
    final_state = stable_file(
        paths["pre_boundary"], max_bytes=MAX_JSON_BYTES, collect=False
    )
    if not exact_json_equal(
        {key: pre_state[key] for key in ("identity", "length", "last_write_ns", "sha256")},
        {key: final_state[key] for key in ("identity", "length", "last_write_ns", "sha256")},
    ):
        raise BootstrapError("pre-boundary report changed during save inventory proof")


def verify_preconfig_environment_backups(
    roots: Roots, manifest: Mapping[str, Any]
) -> None:
    backup = manifest.get("backup") if isinstance(manifest.get("backup"), Mapping) else {}
    configs = backup.get("configs")
    if not isinstance(configs, list) or len(configs) != len(CONFIG_RELATIVES):
        raise BootstrapError("environment config backup manifest is incomplete")
    seen: set[str] = set()
    expected_backup_nodes: set[str] = set()

    def include_backup_path(relative: pathlib.PurePosixPath) -> None:
        parts = list(relative.parts)
        for count in range(1, len(parts) + 1):
            expected_backup_nodes.add(pathlib.PurePosixPath(*parts[:count]).as_posix())

    for record in configs:
        if not isinstance(record, Mapping) or set(record) != {
            "relative",
            "path_id_lower",
            "path_id_upper",
            "exists",
            "state",
            "backup_relative",
        }:
            # Absent records omit state in the environment manager contract.
            if not isinstance(record, Mapping) or set(record) != {
                "relative",
                "path_id_lower",
                "path_id_upper",
                "exists",
                "backup_relative",
            }:
                raise BootstrapError("environment config backup record shape is invalid")
        relative = str(record.get("relative") or "")
        if relative not in CONFIG_RELATIVES or relative in seen:
            raise BootstrapError("environment config backup pathset is invalid")
        seen.add(relative)
        live = roots.oni_root / pathlib.Path(*_safe_relative(relative, "config").parts)
        if (
            record.get("path_id_lower") != path_id(live)
            or record.get("path_id_upper") != path_id(live, upper=True)
            or type(record.get("exists")) is not bool
        ):
            raise BootstrapError("environment config backup binding is invalid")
        current = optional_compact_file_state(live)
        expected_current = (
            {"exists": True, "state": record.get("state")}
            if record.get("exists") is True
            else {"exists": False}
        )
        if not exact_json_equal(current, expected_current):
            raise BootstrapError("pre-config abort requires every config to remain unchanged")
        if record.get("exists") is True:
            backup_relative = _safe_relative(record.get("backup_relative"), "config backup")
            backup_path = roots.environment_dir / pathlib.Path(*backup_relative.parts)
            if not exact_json_equal(compact_file_state(backup_path), record.get("state")):
                raise BootstrapError("environment config backup changed")
            include_backup_path(backup_relative)
        elif record.get("backup_relative") is not None:
            raise BootstrapError("absent config unexpectedly names a backup")
    for key in ("probe", "old_bridge"):
        record = backup.get(key)
        if not isinstance(record, Mapping):
            raise BootstrapError("environment tree backup record is invalid")
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise BootstrapError("environment tree backup snapshot is invalid")
        if snapshot.get("exists") is True:
            relative = _safe_relative(record.get("backup_relative"), f"{key} backup")
            if not tree_matches_snapshot(
                roots.environment_dir / pathlib.Path(*relative.parts), snapshot
            ):
                raise BootstrapError(f"environment {key} backup changed")
            include_backup_path(relative)
            for item in snapshot.get("entries", []):
                include_backup_path(
                    relative
                    / pathlib.PurePosixPath(str(item.get("relative") or ""))
                )
    backup_root = roots.environment_dir / "backup"
    if expected_backup_nodes:
        if not os.path.lexists(backup_root) or is_reparse(backup_root) or not backup_root.is_dir():
            raise BootstrapError("environment backup root is missing or unsafe")
        actual_nodes = {
            node.relative_to(roots.environment_dir).as_posix()
            for node in _logical_tree_paths(backup_root)
        }
        actual_nodes.add("backup")
        if actual_nodes != expected_backup_nodes:
            raise BootstrapError("environment backup pathset has an unexpected/missing node")


def _abort_explicit_paths(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
) -> dict[str, pathlib.Path]:
    required = {
        "pre_boundary": getattr(args, "pre_boundary", None),
        "clone_manifest": getattr(args, "clone_manifest", None),
        "source_save": getattr(args, "source_save", None),
        "loader_save": getattr(args, "loader_save", None),
    }
    if any(not isinstance(value, str) or not value for value in required.values()):
        raise BootstrapError(
            "abort requires pre-boundary, clone-manifest, source-save, and loader-save"
        )
    paths = {key: normalized(value) for key, value in required.items()}
    paths["source_preview"] = paths["source_save"].with_suffix(".png")
    paths["loader_preview"] = paths["loader_save"].with_suffix(".png")
    if not path_within(paths["pre_boundary"], roots.evidence_root) or not path_within(
        paths["clone_manifest"], roots.evidence_root
    ):
        raise BootstrapError("abort evidence bindings escaped the evidence root")
    if not path_within(paths["source_save"], roots.oni_root) or not path_within(
        paths["loader_save"], roots.oni_root
    ):
        raise BootstrapError("abort save bindings escaped the ONI root")
    for path in paths.values():
        boundary = roots.oni_root if path_within(path, roots.oni_root) else roots.evidence_root
        assert_no_reparse(path, boundary)
    bindings = manifest.get("bindings") if isinstance(manifest.get("bindings"), Mapping) else {}
    pre_state = stable_file(paths["pre_boundary"], max_bytes=MAX_JSON_BYTES, collect=False)
    clone_state = stable_file(paths["clone_manifest"], max_bytes=MAX_JSON_BYTES, collect=False)
    if (
        bindings.get("pre_boundary", {}).get("path_id") != path_id(paths["pre_boundary"])
        or bindings.get("pre_boundary", {}).get("sha256") != pre_state["sha256"]
        or bindings.get("clone_manifest", {}).get("path_id") != path_id(paths["clone_manifest"])
        or bindings.get("clone_manifest", {}).get("sha256") != clone_state["sha256"]
    ):
        raise BootstrapError("abort pre-boundary/clone manifest binding changed")
    loader_stem = getattr(args, "loader_stem", None)
    if not isinstance(loader_stem, str) or not loader_stem:
        raise BootstrapError("abort requires the exact loader stem")
    if (
        paths["loader_save"].stem != loader_stem
        or bindings.get("loader_save", {}).get("stem_id") != text_id(loader_stem)
    ):
        raise BootstrapError("abort loader stem binding is invalid")
    for key in ("source_save", "source_preview", "loader_save", "loader_preview"):
        binding = bindings.get(key) if isinstance(bindings.get(key), Mapping) else {}
        if (
            binding.get("path_id_lower") != path_id(paths[key])
            or binding.get("path_id_upper") != path_id(paths[key], upper=True)
            or not exact_json_equal(compact_file_state(paths[key]), binding.get("state"))
        ):
            raise BootstrapError(f"abort {key} binding/content changed")
    if same_path(paths["source_save"], paths["loader_save"]):
        raise BootstrapError("abort source and loader saves cannot be the same")
    if (
        compact_file_state(paths["loader_save"])["sha256"]
        != compact_file_state(paths["source_save"])["sha256"]
        or compact_file_state(paths["loader_preview"])["sha256"]
        != compact_file_state(paths["source_preview"])["sha256"]
    ):
        raise BootstrapError("abort loader pair is not byte-identical to its immutable source")
    matching = {
        child.name
        for child in paths["loader_save"].parent.iterdir()
        if child.stem.casefold() == loader_stem.casefold()
        or child.stem.casefold().startswith(loader_stem.casefold() + "-")
    }
    if matching != {paths["loader_save"].name, paths["loader_preview"].name}:
        raise BootstrapError("abort requires exactly one named loader .sav/.png pair")
    _verify_pre_boundary_save_delta(roots, manifest, paths)
    return paths


def _load_install_ready_for_abort(
    args: argparse.Namespace,
    roots: Roots,
    install_plan: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result_path = roots.transaction_dir / "result.json"
    expected = require_hash(
        getattr(args, "expected_install_result_sha256", None),
        "external install ready-result hash",
    )
    result, state = stable_json(result_path, "runtime bootstrap ready result")
    if state["sha256"] != expected:
        raise BootstrapError("install ready result does not match its external hash")
    validate_result(roots, install_plan, result, require_ready_released=True)
    if result.get("status") != "installed_ready" or result.get("shared_lock_released") is not True:
        raise BootstrapError("install ready result is not consumable")
    return result, state


def _require_preconfig_artifact_gate(
    roots: Roots,
    explicit: Mapping[str, pathlib.Path],
    *,
    allow_live_shared_lock: bool,
) -> None:
    environment_allowed = {
        "environment-manifest.json",
        "restore-journal.jsonl",
        "backup",
        "runtime-bootstrap-install-stage.json",
        "runtime-bootstrap-install-stage.json.new",
        "runtime-bootstrap-abort-stage.json",
        "runtime-bootstrap-abort-stage.json.new",
    }
    for node in roots.environment_dir.iterdir():
        if node.name in environment_allowed:
            continue
        if allow_live_shared_lock and node.name == ".finalize.lock":
            continue
        raise BootstrapError(
            "pre-config abort found configuration/startup transaction evidence"
        )
    for node in _logical_tree_paths(roots.oni_root):
        if is_reparse(node):
            raise BootstrapError("pre-config abort ONI scan encountered a reparse point")
        name = node.name.casefold()
        if name.startswith(".g0-") and (name.endswith(".tmp") or "claim" in name):
            raise BootstrapError("pre-config abort found a runtime-config stage/claim")
    allowed_files = {
        normalized(explicit["pre_boundary"]),
        normalized(explicit["clone_manifest"]),
        normalized(roots.environment_dir / "environment-manifest.json"),
        normalized(roots.environment_dir / "restore-journal.jsonl"),
        normalized(roots.environment_dir / "runtime-bootstrap-install-stage.json"),
        normalized(roots.environment_dir / "runtime-bootstrap-abort-stage.json"),
    }
    for node in _logical_tree_paths(roots.evidence_root):
        if is_reparse(node):
            raise BootstrapError("pre-config abort evidence scan encountered a reparse point")
        if not stat.S_ISREG(_lstat(node).st_mode):
            continue
        current = normalized(node)
        if current in allowed_files:
            continue
        if path_within(current, roots.environment_dir, strict=False) or path_within(
            current, roots.transaction_dir, strict=False
        ):
            continue
        if (
            current.parent == roots.transaction_dir.parent
            and re.fullmatch(r"runtime-bootstrap-init-\d{2}\.json(?:\.new)?", current.name)
        ):
            continue
        try:
            relative = current.relative_to(roots.evidence_root)
        except ValueError as exc:
            raise BootstrapError("pre-config evidence scan escaped its root") from exc
        folded_parts = [part.casefold() for part in relative.parts]
        run_folded = roots.run_id.casefold()
        if run_folded in folded_parts or any(run_folded in part for part in folded_parts):
            raise BootstrapError("pre-config abort found run-scoped startup/session evidence")


def _abort_prohibited_evidence_absent(
    roots: Roots,
    manifest: Mapping[str, Any],
    install_plan: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
) -> None:
    if os.path.lexists(roots.environment_dir / "runtime-config-evidence.json"):
        raise BootstrapError("runtime configuration evidence already exists; use normal abort/finalize")
    probe_custody, bridge_custody = _committed_stage_custody(roots, install_plan)
    verify_committed_state(roots, install_plan, probe_custody, bridge_custody)
    # The exact installed Bridge tree has empty command/control/evidence/session
    # surfaces.  Any startup, session, action, command, bundle, or diagnostic
    # publication is therefore rejected by the tree proof above.
    verify_preconfig_environment_backups(roots, manifest)
    _require_preconfig_artifact_gate(
        roots, explicit, allow_live_shared_lock=False
    )


def _abort_file_owner(path: pathlib.Path) -> dict[str, Any]:
    state = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
    return {
        "kind": "file",
        "identity": state["identity"],
        "length": state["length"],
        "last_write_ns": state["last_write_ns"],
        "sha256": state["sha256"],
    }


def _abort_explicit_file_owners(
    paths: Mapping[str, pathlib.Path],
) -> dict[str, dict[str, Any]]:
    """Freeze explicit abort inputs together with their directory custody.

    Content hashes alone do not establish custody: a same-byte replacement can
    otherwise be substituted between WhatIf/stage/apply.  Each binding therefore
    carries the single-link file identity and its parent-directory identity.  A
    second parent capture closes the parent/name-swap window around the stable
    descriptor read.
    """

    owners: dict[str, dict[str, Any]] = {}
    for key, path in sorted(paths.items()):
        boundary = path.parent
        parent_before = capture_directory_custody(path.parent, boundary)
        state = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
        parent_after = capture_directory_custody(path.parent, boundary)
        if not exact_json_equal(
            parent_before["identity"], parent_after["identity"]
        ):
            raise BootstrapError(
                f"abort explicit input {key} parent changed during capture"
            )
        owners[key] = {
            "path_id_lower": path_id(path),
            "path_id_upper": path_id(path, upper=True),
            "parent_path_id_lower": path_id(path.parent),
            "parent_path_id_upper": path_id(path.parent, upper=True),
            "parent_identity": parent_before["identity"],
            "state": {
                "identity": state["identity"],
                "length": state["length"],
                "last_write_ns": state["last_write_ns"],
                "sha256": state["sha256"],
            },
        }
    return owners


def _abort_tree_owner(
    snapshot: Mapping[str, Any], identity: Mapping[str, Any]
) -> dict[str, Any]:
    return {"kind": "tree", "snapshot": dict(snapshot), "identity": dict(identity)}


def _abort_move_step(
    steps: list[dict[str, Any]],
    event: str,
    source_role: str,
    target_role: str,
    owner: Mapping[str, Any],
) -> None:
    steps.append(
        {
            "index": len(steps),
            "event": event,
            "operation": "move",
            "source_role": source_role,
            "target_role": target_role,
            "owner": dict(owner),
        }
    )


def _capture_abort_contract(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    install_plan: Mapping[str, Any],
    install_result: Mapping[str, Any],
    install_result_state: Mapping[str, Any],
    paths: Mapping[str, pathlib.Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    probe_custody, bridge_custody = _committed_stage_custody(roots, install_plan)
    live_probe = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
    live_bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    if not tree_matches_bound_content_identity(
        live_probe, probe_custody["content"], probe_custody["identity"]
    ) or not tree_matches_bound_content_identity(
        live_bridge, bridge_custody["content"], bridge_custody["identity"]
    ):
        raise BootstrapError("abort capture lost installed Probe/Bridge custody")
    archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    archive_snapshot, archive_identity = capture_bound_tree(archive_root)
    old_bridge = install_plan["prestate"]["old_bridge"]
    old_probe = install_plan["prestate"]["probe"]
    archive_pre = install_plan["prestate"]["archive_root"]
    archive_relative = install_plan["desired"].get("archive_relative")
    archive_name = (
        pathlib.PurePosixPath(str(archive_relative)).name
        if isinstance(archive_relative, str) and archive_relative
        else None
    )
    steps: list[dict[str, Any]] = []
    _abort_move_step(
        steps,
        "runtime_bridge_archived",
        "live_bridge",
        "abort_runtime_bridge",
        _abort_tree_owner(
            capture_bound_tree(live_bridge)[0], bridge_custody["identity"]
        ),
    )
    _abort_move_step(
        steps,
        "runtime_probe_archived",
        "live_probe",
        "abort_runtime_probe",
        _abort_tree_owner(
            capture_bound_tree(live_probe)[0], probe_custody["identity"]
        ),
    )
    if archive_pre["snapshot"].get("exists") is not True and archive_snapshot.get("exists") is True:
        _abort_move_step(
            steps,
            "created_archive_root_archived",
            "archive_root",
            "abort_created_archive_root",
            _abort_tree_owner(archive_snapshot, archive_identity),
        )
    if old_bridge["snapshot"].get("exists") is True:
        if archive_name is None:
            raise BootstrapError("abort capture has no old-Bridge archive name")
        _abort_move_step(
            steps,
            "old_bridge_restored",
            (
                "old_bridge_archive"
                if archive_pre["snapshot"].get("exists") is True
                else "relocated_old_bridge_archive"
            ),
            "live_bridge",
            _abort_tree_owner(old_bridge["snapshot"], old_bridge["identity"]),
        )
    if old_probe["snapshot"].get("exists") is True:
        _abort_move_step(
            steps,
            "old_probe_restored",
            "old_probe_recovery",
            "live_probe",
            _abort_tree_owner(old_probe["snapshot"], old_probe["identity"]),
        )
    if archive_pre["snapshot"].get("exists") is True:
        steps.append(
            {
                "index": len(steps),
                "event": "archive_root_mtime_restored",
                "operation": "set_mtime",
                "role": "archive_root",
                "root_identity": archive_pre["identity"]["root_identity"],
                "value": archive_pre["snapshot"]["root_last_write_ns"],
            }
        )
    parent_prestate = install_plan["prestate"]["parent_directories"]
    for event, role, custody in (
        ("probe_parent_mtime_restored", "probe_parent", parent_prestate["probe_parent"]),
        (
            "probe_container_mtime_restored",
            "probe_container",
            parent_prestate["probe_container"],
        ),
        ("oni_root_mtime_restored", "oni_root", parent_prestate["oni_root"]),
    ):
        steps.append(
            {
                "index": len(steps),
                "event": event,
                "operation": "set_mtime",
                "role": role,
                "root_identity": custody["identity"],
                "value": custody["last_write_ns"],
            }
        )
    save_parent_custody = capture_directory_custody(
        paths["loader_save"].parent, roots.oni_root
    )
    explicit_file_owners = _abort_explicit_file_owners(paths)
    seed = {
        "schema": "oni.live-runtime-bootstrap-abort-seed.v1",
        "run_id": roots.run_id,
        "install_transaction_id": install_plan["transaction_id"],
        "install_plan_hash": install_plan["plan_hash"],
        "install_result_sha256": install_result_state["sha256"],
        "install_result_hash": install_result["result_hash"],
        "environment_manifest_sha256": install_plan["inputs"]["environment_manifest"]["sha256"],
        "release_dll_sha256": install_plan["inputs"]["release_artifacts"][ARTIFACT_NAMES[0]]["sha256"],
        "paths": {key: path_id(value) for key, value in sorted(paths.items())},
        "explicit_file_owners": explicit_file_owners,
        "archive_name": archive_name,
        "save_parent_custody": save_parent_custody,
        "steps": steps,
    }
    abort_seed_hash = canonical_hash(seed)
    abort_id = "rtb-abort-" + abort_seed_hash.removeprefix("sha256:")[:24]
    plan: dict[str, Any] = {
        "schema": ABORT_PLAN_SCHEMA,
        "abort_id": abort_id,
        "abort_seed_hash": abort_seed_hash,
        **{key: value for key, value in seed.items() if key != "schema"},
        "full_environment_target": "pre_boundary_with_exact_named_loader_clone",
    }
    plan["plan_hash"] = canonical_hash(plan)
    intent = {
        "schema": ABORT_INTENT_SCHEMA,
        "abort_id": abort_id,
        "run_id": roots.run_id,
        "plan_hash": plan["plan_hash"],
        "install_result_sha256": install_result_state["sha256"],
        "install_result_hash": install_result["result_hash"],
        "environment_manifest_sha256": plan["environment_manifest_sha256"],
        "release_dll_sha256": plan["release_dll_sha256"],
        "confirm_live_run_id": args.confirm_live_run_id,
        "operation": "abort_pre_config_full_environment",
    }
    intent["intent_hash"] = canonical_hash(intent)
    return intent, plan


def _validate_abort_plan_shape(
    roots: Roots,
    plan: Mapping[str, Any],
    install_plan: Mapping[str, Any],
    install_result: Mapping[str, Any],
    install_result_state: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
) -> None:
    required = {
        "schema",
        "abort_id",
        "abort_seed_hash",
        "run_id",
        "install_transaction_id",
        "install_plan_hash",
        "install_result_sha256",
        "install_result_hash",
        "environment_manifest_sha256",
        "release_dll_sha256",
        "paths",
        "explicit_file_owners",
        "archive_name",
        "save_parent_custody",
        "steps",
        "full_environment_target",
        "plan_hash",
    }
    if (
        set(plan) != required
        or plan.get("schema") != ABORT_PLAN_SCHEMA
        or plan.get("run_id") != roots.run_id
        or plan.get("install_transaction_id") != install_plan["transaction_id"]
        or plan.get("install_plan_hash") != install_plan["plan_hash"]
        or plan.get("install_result_sha256") != install_result_state["sha256"]
        or plan.get("install_result_hash") != install_result["result_hash"]
        or plan.get("environment_manifest_sha256")
        != install_plan["inputs"]["environment_manifest"]["sha256"]
        or plan.get("release_dll_sha256")
        != install_plan["inputs"]["release_artifacts"][ARTIFACT_NAMES[0]]["sha256"]
        or plan.get("full_environment_target")
        != "pre_boundary_with_exact_named_loader_clone"
        or canonical_hash({key: value for key, value in plan.items() if key != "plan_hash"})
        != plan.get("plan_hash")
        or not isinstance(plan.get("steps"), list)
        or not isinstance(plan.get("paths"), Mapping)
        or not isinstance(plan.get("explicit_file_owners"), Mapping)
        or not isinstance(plan.get("save_parent_custody"), Mapping)
    ):
        raise BootstrapError("pre-config abort plan is not its exact bound contract")
    expected_archive_relative = install_plan["desired"].get("archive_relative")
    expected_archive_name = (
        pathlib.PurePosixPath(expected_archive_relative).name
        if isinstance(expected_archive_relative, str) and expected_archive_relative
        else None
    )
    expected_paths = {
        key: path_id(value) for key, value in sorted(explicit.items())
    }
    expected_explicit_file_owners = _abort_explicit_file_owners(explicit)
    save_parent_custody = plan["save_parent_custody"]
    if (
        plan.get("archive_name") != expected_archive_name
        or not exact_json_equal(plan.get("paths"), expected_paths)
        or not exact_json_equal(
            plan.get("explicit_file_owners"), expected_explicit_file_owners
        )
        or set(save_parent_custody) != {"identity", "last_write_ns"}
        or not _valid_identity_list(save_parent_custody.get("identity"))
        or type(save_parent_custody.get("last_write_ns")) is not int
    ):
        raise BootstrapError("pre-config abort fixed path/custody contract is invalid")
    for index, step in enumerate(plan["steps"]):
        if (
            not isinstance(step, Mapping)
            or type(step.get("index")) is not int
            or step.get("index") != index
            or not isinstance(step.get("event"), str)
        ):
            raise BootstrapError("pre-config abort plan step order is invalid")
        if step.get("operation") == "move":
            if set(step) != {
                "index",
                "event",
                "operation",
                "source_role",
                "target_role",
                "owner",
            } or step.get("owner", {}).get("kind") not in {"file", "tree"}:
                raise BootstrapError("pre-config abort move step is invalid")
        elif step.get("operation") == "set_mtime":
            if (
                set(step)
                != {
                    "index",
                    "event",
                    "operation",
                    "role",
                    "root_identity",
                    "value",
                }
                or not _valid_identity_list(step.get("root_identity"))
                or type(step.get("value")) is not int
            ):
                raise BootstrapError("pre-config abort mtime step is invalid")
        else:
            raise BootstrapError("pre-config abort plan operation is invalid")
    seed = {
        "schema": "oni.live-runtime-bootstrap-abort-seed.v1",
        "run_id": roots.run_id,
        "install_transaction_id": install_plan["transaction_id"],
        "install_plan_hash": install_plan["plan_hash"],
        "install_result_sha256": install_result_state["sha256"],
        "install_result_hash": install_result["result_hash"],
        "environment_manifest_sha256": install_plan["inputs"]["environment_manifest"]["sha256"],
        "release_dll_sha256": install_plan["inputs"]["release_artifacts"][ARTIFACT_NAMES[0]]["sha256"],
        "paths": expected_paths,
        "explicit_file_owners": expected_explicit_file_owners,
        "archive_name": expected_archive_name,
        "save_parent_custody": dict(save_parent_custody),
        "steps": [dict(step) for step in plan["steps"]],
    }
    seed_hash = canonical_hash(seed)
    abort_id = "rtb-abort-" + seed_hash.removeprefix("sha256:")[:24]
    expected_plan = {
        "schema": ABORT_PLAN_SCHEMA,
        "abort_id": abort_id,
        "abort_seed_hash": seed_hash,
        **{key: value for key, value in seed.items() if key != "schema"},
        "full_environment_target": "pre_boundary_with_exact_named_loader_clone",
    }
    expected_plan["plan_hash"] = canonical_hash(expected_plan)
    if not exact_json_equal(dict(plan), expected_plan):
        raise BootstrapError("pre-config abort deterministic plan reconstruction failed")


def _validate_abort_intent(
    args: argparse.Namespace,
    roots: Roots,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    expected = {
        "schema": ABORT_INTENT_SCHEMA,
        "abort_id": plan["abort_id"],
        "run_id": roots.run_id,
        "plan_hash": plan["plan_hash"],
        "install_result_sha256": plan["install_result_sha256"],
        "install_result_hash": plan["install_result_hash"],
        "environment_manifest_sha256": plan["environment_manifest_sha256"],
        "release_dll_sha256": plan["release_dll_sha256"],
        "confirm_live_run_id": args.confirm_live_run_id,
        "operation": "abort_pre_config_full_environment",
    }
    expected["intent_hash"] = canonical_hash(expected)
    if not exact_json_equal(dict(intent), expected):
        raise BootstrapError("pre-config abort intent is not canonical/exact")


def _abort_stage_document(
    roots: Roots,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    stage_nonce: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{32}", stage_nonce) is None:
        raise BootstrapError("abort stage nonce must be exactly 32 lowercase hex")
    producer = stable_file(pathlib.Path(__file__).resolve(), max_bytes=MAX_JSON_BYTES, collect=False)
    return _seal_contract_document(
        {
            "schema": ABORT_STAGE_SCHEMA,
            "run_id": roots.run_id,
            "abort_id": plan["abort_id"],
            "plan_hash": plan["plan_hash"],
            "abort_intent_hash": intent["intent_hash"],
            "stage_nonce": stage_nonce,
            "abort_intent": dict(intent),
            "abort_plan": dict(plan),
            "producer": {
                "length": producer["length"],
                "sha256": producer["sha256"],
            },
        },
        "stage_hash",
    )


def _load_abort_stage(
    roots: Roots,
    *,
    expected_file_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    path = roots.environment_dir / "runtime-bootstrap-abort-stage.json"
    value, state = stable_json(path, "pre-config abort stage authorization")
    if state.get("sha256") != require_hash(
        expected_file_sha256, "external abort stage raw hash"
    ):
        raise BootstrapError("abort stage authorization does not match its external hash")
    intent = value.get("abort_intent")
    plan = value.get("abort_plan")
    nonce = value.get("stage_nonce")
    if not isinstance(intent, Mapping) or not isinstance(plan, Mapping) or not isinstance(nonce, str):
        raise BootstrapError("abort stage authorization bundle is incomplete")
    expected = _abort_stage_document(roots, intent, plan, nonce)
    if not exact_json_equal(value, expected):
        raise BootstrapError("abort stage authorization is not exact")
    return dict(value), state, dict(intent), dict(plan)


def _abort_role_paths(
    roots: Roots,
    plan: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
) -> dict[str, pathlib.Path]:
    abort_root = roots.transaction_dir / "abort"
    custody = abort_root / "custody"
    archive_name = plan.get("archive_name")
    archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    result = {
        "live_bridge": roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts),
        "abort_runtime_bridge": custody / "b",
        "live_probe": roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts),
        "abort_runtime_probe": custody / "p",
        "archive_root": archive_root,
        "abort_created_archive_root": custody / "a",
        "old_probe_recovery": roots.transaction_dir / "recovery" / "old-probe",
        "oni_root": roots.oni_root,
        "probe_parent": (
            roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
        ).parent,
        "probe_container": (
            roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
        ).parent.parent,
        "save_parent": explicit["loader_save"].parent,
        "loader_save": explicit["loader_save"],
        "loader_preview": explicit["loader_preview"],
        "clone_manifest": explicit["clone_manifest"],
    }
    if isinstance(archive_name, str) and archive_name:
        result["old_bridge_archive"] = archive_root / archive_name
        result["relocated_old_bridge_archive"] = (
            custody / "a" / archive_name
        )
    return result


def _abort_step_data(step: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "step_index": step["index"],
        "step_hash": canonical_hash(step),
    }


def _abort_journal_events(plan: Mapping[str, Any]) -> list[str]:
    events = ["abort_initialized"]
    for step in plan["steps"]:
        event = str(step["event"])
        if step.get("operation") == "set_mtime":
            events.append(event + "_claim_prepared")
        events.append(event)
    events.append("abort_commit_verified")
    return events


def _abort_mtime_prepared_data(
    step: Mapping[str, Any], owner: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "step_index": step["index"],
        "step_hash": canonical_hash(step),
        "owner": dict(owner),
    }


def _abort_mtime_expected_owner(
    abort_root: pathlib.Path,
    plan: Mapping[str, Any],
    step: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    event = str(step["event"]) + "_claim_prepared"
    for entry in _verify_abort_journal(abort_root, plan):
        if entry.get("event") == event:
            owner = entry.get("data", {}).get("owner")
            return dict(owner) if isinstance(owner, Mapping) else None
    return None


def _abort_owner_with_mtime(
    owner: Mapping[str, Any], value: int
) -> dict[str, Any]:
    committed_owner = copy.deepcopy(dict(owner))
    if committed_owner.get("kind") != "directory" or not isinstance(
        committed_owner.get("tree"), Mapping
    ):
        raise BootstrapError("pre-config abort mtime owner is invalid")
    committed_tree = dict(committed_owner["tree"])
    committed_tree["root_last_write_ns"] = int(value)
    committed_tree_without_hash = dict(committed_tree)
    committed_tree_without_hash.pop("tree_hash", None)
    committed_tree["tree_hash"] = sha256_bytes(
        canonical_bytes(committed_tree_without_hash)
    )
    committed_owner["tree"] = committed_tree
    return committed_owner


def _abort_completed_prefix_guard(
    roots: Roots,
    abort_root: pathlib.Path,
    plan: Mapping[str, Any],
    roles: Mapping[str, pathlib.Path],
) -> set[str]:
    entries = _verify_abort_journal(abort_root, plan)
    completed = {
        str(entry["event"])
        for entry in entries
        if any(step.get("event") == entry.get("event") for step in plan["steps"])
    }
    old_bridge_completed = "old_bridge_restored" in completed
    pending_steps = [
        step
        for step in plan["steps"]
        if str(step.get("event")) not in completed
    ]
    next_pending_step = pending_steps[0] if pending_steps else None
    for step in plan["steps"]:
        event = str(step["event"])
        if event not in completed:
            continue
        if step.get("operation") == "move":
            target = roles[str(step["target_role"])]
            owner = step["owner"]
            if step.get("target_role") == "abort_created_archive_root" and old_bridge_completed:
                try:
                    snapshot, identity = capture_bound_tree(target)
                    valid = (
                        snapshot.get("exists") is True
                        and snapshot.get("entries") == []
                        and exact_json_equal(
                            identity.get("root_identity"),
                            owner.get("identity", {}).get("root_identity"),
                        )
                    )
                except BootstrapError:
                    valid = False
            elif (
                step.get("target_role") == "abort_created_archive_root"
                and isinstance(next_pending_step, Mapping)
                and next_pending_step.get("event") == "old_bridge_restored"
                and next_pending_step.get("operation") == "move"
                and next_pending_step.get("source_role")
                == "relocated_old_bridge_archive"
                and next_pending_step.get("target_role") == "live_bridge"
                and type(next_pending_step.get("index")) is int
                and type(step.get("index")) is int
                and next_pending_step["index"] == step["index"] + 1
            ):
                valid = _abort_owner_matches(target, owner)
                if not valid:
                    # The old-Bridge restore may have durably consumed the
                    # sole direct child of the already-journaled created
                    # archive-root, while crashing before its own journal
                    # publication. Accept that transformed custody only as a
                    # complete four-location sandwich; the step executor will
                    # append the missing journal entry without replaying
                    # rename. The ordinary pre-step state remains the exact
                    # original step-2 owner above.
                    relocated = roles[str(next_pending_step["source_role"])]
                    live_bridge = roles[str(next_pending_step["target_role"])]
                    direct_consumers = [
                        later
                        for later in plan["steps"]
                        if type(later.get("index")) is int
                        and later["index"] > step["index"]
                        and later.get("operation") == "move"
                        and isinstance(later.get("source_role"), str)
                        and same_path(
                            roles[str(later["source_role"])].parent,
                            target,
                        )
                    ]
                    try:
                        snapshot, identity = capture_bound_tree(target)
                        valid = (
                            len(direct_consumers) == 1
                            and exact_json_equal(
                                direct_consumers[0], next_pending_step
                            )
                            and same_path(relocated.parent, target)
                            and relocated.name == plan.get("archive_name")
                            and not os.path.lexists(relocated)
                            and snapshot.get("exists") is True
                            and snapshot.get("entries") == []
                            and exact_json_equal(
                                identity.get("root_identity"),
                                owner.get("identity", {}).get("root_identity"),
                            )
                            and _abort_owner_matches(
                                live_bridge, next_pending_step["owner"]
                            )
                        )
                    except (BootstrapError, KeyError, TypeError):
                        valid = False
            else:
                valid = _abort_owner_matches(target, owner)
            if not valid:
                raise BootstrapError(
                    f"completed pre-config abort step {event} lost exact custody"
                )
        else:
            before_owner = _abort_mtime_expected_owner(abort_root, plan, step)
            if before_owner is None:
                raise BootstrapError(
                    "completed pre-config abort mtime lacks its prepared owner"
                )
            expected = _abort_owner_with_mtime(before_owner, int(step["value"]))
            path = roles[str(step["role"])]
            if not environment_core.transaction_node_matches(path, expected):
                raise BootstrapError(
                    f"completed pre-config abort step {event} lost mtime custody"
                )
    return completed


def _verify_abort_journal(
    abort_root: pathlib.Path, plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    journal = abort_root / "journal"
    if not os.path.lexists(journal) or is_reparse(journal) or not journal.is_dir():
        raise BootstrapError("pre-config abort journal root is unsafe")
    nodes = sorted(journal.iterdir(), key=lambda item: item.name)
    expected_events = _abort_journal_events(plan)
    partials = [item for item in nodes if item.name.endswith(".json.new")]
    if len(partials) > 1:
        raise BootstrapError("pre-config abort journal has multiple partial publications")
    files = [item for item in nodes if not item.name.endswith(".new")]
    if len(partials) == 1:
        next_index = len(files)
        if next_index >= len(expected_events) or partials[0].name != (
            f"{next_index:04d}-{expected_events[next_index]}.json.new"
        ):
            raise BootstrapError("pre-config abort journal partial publication is out of order")
    entries: list[dict[str, Any]] = []
    previous: Optional[str] = None
    for index, path in enumerate(files):
        if index >= len(expected_events):
            raise BootstrapError("pre-config abort journal has extra entries")
        event = expected_events[index]
        if path.name != f"{index:04d}-{event}.json":
            raise BootstrapError("pre-config abort journal filename/order is invalid")
        entry, _ = stable_json(path, "pre-config abort journal entry")
        if (
            set(entry)
            != {
                "schema",
                "index",
                "abort_id",
                "plan_hash",
                "event",
                "data",
                "previous_entry_hash",
                "entry_hash",
            }
            or entry.get("schema") != ABORT_JOURNAL_SCHEMA
            or type(entry.get("index")) is not int
            or entry.get("index") != index
            or entry.get("abort_id") != plan["abort_id"]
            or entry.get("plan_hash") != plan["plan_hash"]
            or entry.get("event") != event
            or entry.get("previous_entry_hash") != previous
            or not isinstance(entry.get("data"), Mapping)
        ):
            raise BootstrapError("pre-config abort journal entry shape is invalid")
        if index == 0:
            if set(entry["data"]) != {
                "owner",
                "intent",
                "plan",
                "stage_sha256",
                "stage_hash",
            }:
                raise BootstrapError("pre-config abort journal origin data is invalid")
            for label in ("owner", "intent", "plan"):
                record = entry["data"].get(label)
                if (
                    not isinstance(record, Mapping)
                    or set(record) != {"identity", "length", "last_write_ns", "sha256"}
                    or not _valid_identity_list(record.get("identity"))
                    or type(record.get("length")) is not int
                    or type(record.get("last_write_ns")) is not int
                    or HASH_RE.fullmatch(str(record.get("sha256") or "")) is None
                ):
                    raise BootstrapError("pre-config abort journal publication record is invalid")
            if (
                HASH_RE.fullmatch(str(entry["data"].get("stage_sha256") or "")) is None
                or HASH_RE.fullmatch(str(entry["data"].get("stage_hash") or "")) is None
            ):
                raise BootstrapError("pre-config abort journal stage anchor is invalid")
            owner_value, _ = stable_json(
                abort_root / "owner.json", "pre-config abort owner"
            )
            expected_origin = {
                "owner": file_record(abort_root / "owner.json"),
                "intent": file_record(abort_root / "intent.json"),
                "plan": file_record(abort_root / "plan.json"),
                "stage_sha256": owner_value.get("stage_sha256"),
                "stage_hash": owner_value.get("stage_hash"),
            }
            if not exact_json_equal(entry["data"], expected_origin):
                raise BootstrapError("pre-config abort journal origin publication changed")
        elif event.endswith("_claim_prepared"):
            matching_steps = [
                step
                for step in plan["steps"]
                if step.get("operation") == "set_mtime"
                and str(step.get("event")) + "_claim_prepared" == event
            ]
            owner = entry["data"].get("owner")
            if (
                len(matching_steps) != 1
                or set(entry["data"]) != {"step_index", "step_hash", "owner"}
                or not isinstance(owner, Mapping)
                or owner.get("kind") != "directory"
                or not exact_json_equal(
                    owner.get("identity"), matching_steps[0].get("root_identity")
                )
                or not exact_json_equal(
                    {
                        "step_index": entry["data"].get("step_index"),
                        "step_hash": entry["data"].get("step_hash"),
                    },
                    _abort_step_data(matching_steps[0]),
                )
            ):
                raise BootstrapError(
                    "pre-config abort mtime-claim journal data is invalid"
                )
        elif event != "abort_commit_verified":
            matching_steps = [
                step for step in plan["steps"] if step.get("event") == event
            ]
            if len(matching_steps) != 1 or not exact_json_equal(
                entry["data"], _abort_step_data(matching_steps[0])
            ):
                raise BootstrapError("pre-config abort journal step data is invalid")
        else:
            if (
                set(entry["data"])
                != {"final_state_hash", "step_count", "full_environment_restored"}
                or HASH_RE.fullmatch(str(entry["data"].get("final_state_hash") or "")) is None
                or type(entry["data"].get("step_count")) is not int
                or entry["data"].get("step_count") != len(plan["steps"])
                or entry["data"].get("full_environment_restored") is not True
            ):
                raise BootstrapError("pre-config abort commit data is invalid")
        unhashed = dict(entry)
        claimed = unhashed.pop("entry_hash", None)
        if canonical_hash(unhashed) != claimed:
            raise BootstrapError("pre-config abort journal hash chain is invalid")
        previous = claimed
        entries.append(entry)
    return entries


def _append_abort_journal(
    abort_root: pathlib.Path,
    plan: Mapping[str, Any],
    event: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    entries = _verify_abort_journal(abort_root, plan)
    expected_events = _abort_journal_events(plan)
    if event not in expected_events:
        raise BootstrapError("unknown pre-config abort journal event")
    expected_index = expected_events.index(event)
    if expected_index < len(entries):
        existing = entries[expected_index]
        if not exact_json_equal(existing["data"], dict(data)):
            raise BootstrapError("pre-config abort journal replay data conflicts")
        return existing
    if expected_index != len(entries):
        raise BootstrapError("pre-config abort journal transition is out of order")
    entry: dict[str, Any] = {
        "schema": ABORT_JOURNAL_SCHEMA,
        "index": expected_index,
        "abort_id": plan["abort_id"],
        "plan_hash": plan["plan_hash"],
        "event": event,
        "data": dict(data),
        "previous_entry_hash": entries[-1]["entry_hash"] if entries else None,
    }
    entry["entry_hash"] = canonical_hash(entry)
    write_json_create_only(
        abort_root / "journal" / f"{expected_index:04d}-{event}.json",
        entry,
        f"pre-config abort journal {event}",
    )
    return entry


def _initialize_abort_transaction(
    args: argparse.Namespace,
    roots: Roots,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    abort_stage: Mapping[str, Any],
    abort_stage_state: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> None:
    abort_root = roots.transaction_dir / "abort"
    owner_path = abort_root / "owner.json"
    install_owner, _ = stable_json(
        roots.transaction_dir / "owner.json", "runtime bootstrap owner"
    )
    support = {
        "root_identity": (
            abort_root,
            install_owner.get("abort_root_identity"),
        ),
        "custody_identity": (
            abort_root / "custody",
            install_owner.get("abort_custody_identity"),
        ),
        "journal_identity": (
            abort_root / "journal",
            install_owner.get("abort_journal_identity"),
        ),
    }
    for label, (path, expected_identity) in support.items():
        if (
            not os.path.lexists(path)
            or is_reparse(path)
            or not path.is_dir()
            or node_identity(os.lstat(path)) != expected_identity
        ):
            raise BootstrapError(f"pre-config abort {label} support custody changed")
    expected_owner = _seal_contract_document({
        "schema": ABORT_OWNER_SCHEMA,
        "abort_id": plan["abort_id"],
        "run_id": roots.run_id,
        "plan_hash": plan["plan_hash"],
        "root_identity": node_identity(os.lstat(abort_root)),
        "custody_identity": node_identity(os.lstat(abort_root / "custody")),
        "journal_identity": node_identity(os.lstat(abort_root / "journal")),
        "stage_sha256": abort_stage_state["sha256"],
        "stage_hash": abort_stage["stage_hash"],
    }, "owner_hash")
    write_json_create_only(owner_path, expected_owner, "pre-config abort owner")
    checkpoint(args, "after_abort_owner_publish", hook)
    owner, _ = stable_json(owner_path, "pre-config abort owner")
    if not exact_json_equal(owner, expected_owner):
        raise BootstrapError("pre-config abort ownership was name-swapped")
    write_json_create_only(abort_root / "intent.json", intent, "pre-config abort intent")
    checkpoint(args, "after_abort_intent_publish", hook)
    write_json_create_only(abort_root / "plan.json", plan, "pre-config abort plan")
    checkpoint(args, "after_abort_plan_publish", hook)
    _append_abort_journal(
        abort_root,
        plan,
        "abort_initialized",
        {
            "owner": file_record(owner_path),
            "intent": file_record(abort_root / "intent.json"),
            "plan": file_record(abort_root / "plan.json"),
            "stage_sha256": abort_stage_state["sha256"],
            "stage_hash": abort_stage["stage_hash"],
        },
    )
    checkpoint(args, "after_abort_journal_initialized", hook)


def _abort_owner_matches(path: pathlib.Path, owner: Mapping[str, Any]) -> bool:
    try:
        if owner.get("kind") == "tree":
            return optional_tree_state(path, owner["snapshot"], owner["identity"])
        if owner.get("kind") == "file":
            state = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
            return exact_json_equal(
                {
                    "kind": "file",
                    "identity": state["identity"],
                    "length": state["length"],
                    "last_write_ns": state["last_write_ns"],
                    "sha256": state["sha256"],
                },
                dict(owner),
            )
    except (BootstrapError, OSError):
        return False
    return False


def _abort_external_guard(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    install_plan: Mapping[str, Any],
    install_result: Mapping[str, Any],
    install_result_state: Mapping[str, Any],
    abort_plan: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
) -> None:
    require_processes_stopped(args, roots)
    validate_manifest_anchor(args, roots, install_plan)
    current_release, _ = release_artifacts(args, roots)
    if not exact_json_equal(
        current_release, install_plan["inputs"]["release_artifacts"]
    ):
        raise BootstrapError("release artifact anchors changed during pre-config abort")
    ready, state = stable_json(
        roots.transaction_dir / "result.json", "runtime bootstrap ready result"
    )
    if (
        state["sha256"] != install_result_state["sha256"]
        or not exact_json_equal(ready, install_result)
    ):
        raise BootstrapError("install ready result changed during pre-config abort")
    validate_result(roots, install_plan, ready, require_ready_released=True)
    verify_preconfig_environment_backups(roots, manifest)
    if os.path.lexists(roots.environment_dir / "runtime-config-evidence.json"):
        raise BootstrapError("runtime configuration evidence appeared during pre-config abort")
    _require_preconfig_artifact_gate(
        roots, explicit, allow_live_shared_lock=True
    )
    bindings = manifest["bindings"]
    if (
        stable_file(explicit["pre_boundary"], max_bytes=MAX_JSON_BYTES, collect=False)["sha256"]
        != bindings["pre_boundary"]["sha256"]
        or not exact_json_equal(
            compact_file_state(explicit["source_save"]), bindings["source_save"]["state"]
        )
        or not exact_json_equal(
            compact_file_state(explicit["source_preview"]),
            bindings["source_preview"]["state"],
        )
    ):
        raise BootstrapError("pre-boundary/source anchors changed during abort")
    rebound = _abort_explicit_paths(args, roots, manifest)
    if any(not same_path(rebound[key], explicit[key]) for key in explicit):
        raise BootstrapError("pre-config abort explicit path binding changed")
    if not exact_json_equal(
        _abort_explicit_file_owners(rebound),
        abort_plan["explicit_file_owners"],
    ):
        raise BootstrapError(
            "pre-config abort explicit input identity/parent custody changed"
        )
    save_parent = capture_directory_custody(
        explicit["loader_save"].parent, roots.oni_root
    )
    if not exact_json_equal(save_parent, abort_plan["save_parent_custody"]):
        raise BootstrapError("loader save parent custody/mtime changed during abort")


def _execute_abort_step(
    args: argparse.Namespace,
    roots: Roots,
    abort_root: pathlib.Path,
    plan: Mapping[str, Any],
    step: Mapping[str, Any],
    roles: Mapping[str, pathlib.Path],
    hook: Optional[Callable[[str], None]],
) -> None:
    event = str(step["event"])
    entries = _verify_abort_journal(abort_root, plan)
    already_journaled = any(entry.get("event") == event for entry in entries)
    if step["operation"] == "move":
        source = roles[str(step["source_role"])]
        target = roles[str(step["target_role"])]
        owner = step["owner"]
        at_source = _abort_owner_matches(source, owner)
        at_target = _abort_owner_matches(target, owner)
        if (
            not at_target
            and already_journaled
            and step.get("target_role") == "abort_created_archive_root"
            and os.path.lexists(target)
        ):
            try:
                transformed_snapshot, transformed_identity = capture_bound_tree(target)
                at_target = (
                    transformed_snapshot.get("exists") is True
                    and transformed_snapshot.get("entries") == []
                    and exact_json_equal(
                        transformed_identity.get("root_identity"),
                        owner.get("identity", {}).get("root_identity"),
                    )
                )
            except BootstrapError:
                at_target = False
        if at_source and not os.path.lexists(target):
            if already_journaled:
                raise BootstrapError("journaled abort move reverted to its source")
            checkpoint(args, f"before_abort_{event}_physical", hook)
            # A process may start in the checkpoint/concurrency window.  No
            # pathname is claimed until this fresh census succeeds.
            require_processes_stopped(args, roots)
            if owner["kind"] == "tree":
                rename_bound_tree(
                    source,
                    target,
                    expected_identity=owner["identity"],
                    expected_snapshot=owner["snapshot"],
                    label=f"pre-config abort {event}",
                )
            else:
                source_state = stable_file(
                    source, max_bytes=MAX_ARTIFACT_BYTES, collect=True
                )
                payload = source_state.get("bytes")
                if not isinstance(payload, bytes):
                    raise BootstrapError("pre-config abort file move lost its payload")
                rename_bound_file(
                    source,
                    target,
                    expected_identity=owner["identity"],
                    expected_payload=payload,
                    label=f"pre-config abort {event}",
                )
            checkpoint(args, f"after_abort_{event}_physical", hook)
            at_target = _abort_owner_matches(target, owner)
        else:
            known_later_occupant = False
            if os.path.lexists(source):
                known_later_occupant = any(
                    type(later.get("index")) is int
                    and int(later["index"]) > int(step["index"])
                    and later.get("operation") == "move"
                    and later.get("target_role") == step.get("source_role")
                    and isinstance(later.get("owner"), Mapping)
                    and _abort_owner_matches(source, later["owner"])
                    for later in plan["steps"]
                )
            if not at_target or (
                os.path.lexists(source) and not known_later_occupant
            ):
                raise BootstrapError(
                    f"pre-config abort step {event} has ambiguous custody"
                )
        if not at_target:
            raise BootstrapError(f"pre-config abort step {event} failed custody proof")
    else:
        path = roles[str(step["role"])]
        prepared_event = event + "_claim_prepared"
        prepared_owner = _abort_mtime_expected_owner(abort_root, plan, step)
        if prepared_owner is None:
            try:
                prepared_owner = environment_core.capture_transaction_node(path)
            except (OSError, environment_core.WorkflowError) as exc:
                raise BootstrapError(
                    "pre-config abort mtime owner could not be captured"
                ) from exc
            if (
                prepared_owner.get("kind") != "directory"
                or not exact_json_equal(
                    prepared_owner.get("identity"), step["root_identity"]
                )
            ):
                raise BootstrapError("pre-config abort mtime target lost identity")
            _append_abort_journal(
                abort_root,
                plan,
                prepared_event,
                _abort_mtime_prepared_data(step, prepared_owner),
            )
            checkpoint(args, f"after_abort_{prepared_event}_journal", hook)
        committed_owner = _abort_owner_with_mtime(
            prepared_owner, int(step["value"])
        )
        at_path_before = environment_core.transaction_node_matches(
            path, prepared_owner
        )
        at_path_after = environment_core.transaction_node_matches(
            path, committed_owner
        )
        if already_journaled:
            if not at_path_after:
                raise BootstrapError("journaled abort mtime changed")
        elif at_path_after:
            # SetFileTime was durable but the step journal publication was
            # interrupted. Only the journal transition remains.
            pass
        else:
            if not at_path_before:
                raise BootstrapError(
                    "pre-config abort mtime pathname has an external replacement"
                )
            checkpoint(args, f"before_abort_{event}_physical", hook)
            require_processes_stopped(args, roots)
            set_directory_mtime_by_owned_handle(
                path,
                step["root_identity"],
                int(step["value"]),
                after_open=lambda: checkpoint(
                    args, f"after_abort_{event}_handle_open", hook
                ),
                before_set=lambda: require_processes_stopped(args, roots),
                after_set=lambda: checkpoint(
                    args, f"after_abort_{event}_handle_settime", hook
                ),
            )
            if not environment_core.transaction_node_matches(path, committed_owner):
                raise BootstrapError(
                    "pre-config abort mtime handle restore is outcome_unknown"
                )
            checkpoint(args, f"after_abort_{event}_physical", hook)
    _append_abort_journal(abort_root, plan, event, _abort_step_data(step))
    checkpoint(args, f"after_abort_{event}_journal", hook)


def _abort_final_state(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    install_plan: Mapping[str, Any],
    abort_plan: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
) -> dict[str, Any]:
    verify_preconfig_environment_backups(roots, manifest)
    prestate = install_plan["prestate"]
    live_probe = roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts)
    live_bridge = roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts)
    archive_root = roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts)
    if not optional_tree_state(
        live_probe, prestate["probe"]["snapshot"], prestate["probe"]["identity"]
    ):
        raise BootstrapError("pre-config abort did not restore original Probe")
    if not optional_tree_state(
        live_bridge,
        prestate["old_bridge"]["snapshot"],
        prestate["old_bridge"]["identity"],
    ):
        raise BootstrapError("pre-config abort did not restore original Bridge")
    if not optional_tree_state(
        archive_root,
        prestate["archive_root"]["snapshot"],
        prestate["archive_root"]["identity"],
    ):
        raise BootstrapError("pre-config abort did not restore original archive root")
    rebound = _abort_explicit_paths(args, roots, manifest)
    if any(not same_path(rebound[key], explicit[key]) for key in explicit):
        raise BootstrapError("pre-config abort changed a loader-clone binding path")
    if not exact_json_equal(
        _abort_explicit_file_owners(rebound),
        abort_plan["explicit_file_owners"],
    ):
        raise BootstrapError(
            "pre-config abort did not preserve explicit input identity/parent custody"
        )
    if not exact_json_equal(
        capture_directory_custody(explicit["loader_save"].parent, roots.oni_root),
        abort_plan["save_parent_custody"],
    ):
        raise BootstrapError("pre-config abort changed the loader save parent")
    parent_prestate = install_plan["prestate"]["parent_directories"]
    for role, path in (
        ("oni_root", roots.oni_root),
        ("probe_container", live_probe.parent.parent),
        ("probe_parent", live_probe.parent),
    ):
        if not exact_json_equal(
            capture_directory_custody(path, roots.oni_root), parent_prestate[role]
        ):
            raise BootstrapError(f"pre-config abort did not restore {role} mtime/custody")
    roles = _abort_role_paths(roots, abort_plan, explicit)
    custody: dict[str, Any] = {}
    for step in abort_plan["steps"]:
        if step.get("operation") != "move":
            continue
        target_role = str(step["target_role"])
        if target_role in {"live_bridge", "live_probe"}:
            continue
        target = roles[target_role]
        owner = step["owner"]
        if target_role == "abort_created_archive_root":
            snapshot, identity = capture_bound_tree(target)
            if (
                snapshot.get("exists") is not True
                or snapshot.get("entries") != []
                or identity.get("root_identity") != owner["identity"]["root_identity"]
            ):
                raise BootstrapError("archived created archive root retained an unexpected node")
            custody[target_role] = {
                "kind": "tree",
                "content_hash": canonical_hash(tree_content(snapshot)),
                "root_identity": identity["root_identity"],
            }
        else:
            if not _abort_owner_matches(target, owner):
                raise BootstrapError("pre-config abort evidence custody changed")
            custody[target_role] = {
                "kind": owner["kind"],
                "owner_hash": canonical_hash(owner),
            }
    require_processes_stopped(args, roots)
    return {
        "schema": "oni.live-runtime-bootstrap-abort-final-state.v1",
        "run_id": roots.run_id,
        "abort_id": abort_plan["abort_id"],
        "probe_snapshot_hash": canonical_hash(prestate["probe"]["snapshot"]),
        "bridge_snapshot_hash": canonical_hash(prestate["old_bridge"]["snapshot"]),
        "archive_snapshot_hash": canonical_hash(prestate["archive_root"]["snapshot"]),
        "loader_pair_restored": True,
        "clone_manifest_preserved": True,
        "save_parent_custody_hash": canonical_hash(abort_plan["save_parent_custody"]),
        "config_count": len(manifest["backup"]["configs"]),
        "custody": custody,
        "full_environment_restored": True,
        "game_launched": False,
    }


def _active_abort_lock_binding(
    roots: Roots, install_plan: Mapping[str, Any], *, prefix: str
) -> dict[str, Any]:
    return _active_environment_lock_binding(
        roots, install_plan, purpose="abort", prefix=prefix
    )


def _abort_candidate_document(
    roots: Roots,
    install_plan: Mapping[str, Any],
    abort_plan: Mapping[str, Any],
    final_state: Mapping[str, Any],
) -> dict[str, Any]:
    entries = _verify_abort_journal(roots.transaction_dir / "abort", abort_plan)
    commits = [entry for entry in entries if entry.get("event") == "abort_commit_verified"]
    if len(commits) != 1:
        raise BootstrapError("pre-config abort candidate has no unique commit entry")
    abort_owner, _ = stable_json(
        roots.transaction_dir / "abort" / "owner.json", "pre-config abort owner"
    )
    abort_stage, abort_stage_state, _intent, _plan = _load_abort_stage(
        roots, expected_file_sha256=abort_owner["stage_sha256"]
    )
    candidate: dict[str, Any] = {
        "schema": ABORT_CANDIDATE_SCHEMA,
        "status": "candidate_not_ready",
        "classification": "static_probe",
        "run_id": roots.run_id,
        "abort_id": abort_plan["abort_id"],
        "abort_plan_hash": abort_plan["plan_hash"],
        "install_transaction_id": abort_plan["install_transaction_id"],
        "install_plan_hash": abort_plan["install_plan_hash"],
        "install_result_sha256": abort_plan["install_result_sha256"],
        "install_result_hash": abort_plan["install_result_hash"],
        "environment_manifest_sha256": abort_plan["environment_manifest_sha256"],
        "release_dll_sha256": abort_plan["release_dll_sha256"],
        "abort_commit_entry_hash": commits[0]["entry_hash"],
        "final_state_hash": canonical_hash(final_state),
        "abort_stage_sha256": abort_stage_state["sha256"],
        "abort_stage_hash": abort_stage["stage_hash"],
        "producer": abort_stage["producer"],
        **_active_abort_lock_binding(roots, install_plan, prefix="abort"),
        "full_environment_restored": True,
        "loader_pair_restored": True,
        "clone_manifest_preserved": True,
        "physical_restore_complete": True,
        "game_launched": False,
    }
    candidate["candidate_hash"] = canonical_hash(candidate)
    return candidate


def _validate_abort_candidate(
    roots: Roots,
    install_plan: Mapping[str, Any],
    abort_plan: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    require_released_lock: bool,
) -> None:
    entries = _verify_abort_journal(roots.transaction_dir / "abort", abort_plan)
    commits = [entry for entry in entries if entry.get("event") == "abort_commit_verified"]
    if len(commits) != 1:
        raise BootstrapError("pre-config abort candidate commit origin is missing")
    abort_owner, _ = stable_json(
        roots.transaction_dir / "abort" / "owner.json", "pre-config abort owner"
    )
    abort_stage, abort_stage_state, _intent, _plan = _load_abort_stage(
        roots, expected_file_sha256=abort_owner["stage_sha256"]
    )
    expected: dict[str, Any] = {
        "schema": ABORT_CANDIDATE_SCHEMA,
        "status": "candidate_not_ready",
        "classification": "static_probe",
        "run_id": roots.run_id,
        "abort_id": abort_plan["abort_id"],
        "abort_plan_hash": abort_plan["plan_hash"],
        "install_transaction_id": abort_plan["install_transaction_id"],
        "install_plan_hash": abort_plan["install_plan_hash"],
        "install_result_sha256": abort_plan["install_result_sha256"],
        "install_result_hash": abort_plan["install_result_hash"],
        "environment_manifest_sha256": abort_plan["environment_manifest_sha256"],
        "release_dll_sha256": abort_plan["release_dll_sha256"],
        "abort_commit_entry_hash": commits[0]["entry_hash"],
        "final_state_hash": commits[0]["data"]["final_state_hash"],
        "abort_stage_sha256": abort_stage_state["sha256"],
        "abort_stage_hash": abort_stage["stage_hash"],
        "producer": abort_stage["producer"],
        "abort_lock_epoch": candidate.get("abort_lock_epoch"),
        "abort_lock_claim": candidate.get("abort_lock_claim"),
        "abort_lock_claim_hash": candidate.get("abort_lock_claim_hash"),
        "abort_lock_release": candidate.get("abort_lock_release"),
        "abort_lock_release_hash": candidate.get("abort_lock_release_hash"),
        "full_environment_restored": True,
        "loader_pair_restored": True,
        "clone_manifest_preserved": True,
        "physical_restore_complete": True,
        "game_launched": False,
    }
    expected["candidate_hash"] = canonical_hash(expected)
    if not exact_json_equal(dict(candidate), expected):
        raise BootstrapError("pre-config abort candidate is not exact")
    if require_released_lock:
        _validate_certificate_lock_release(
            roots,
            install_plan,
            candidate,
            prefix="abort",
            purpose="abort",
        )


def _abort_result_document(
    roots: Roots,
    install_plan: Mapping[str, Any],
    abort_plan: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_state: Mapping[str, Any],
    ready_binding: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    _validate_abort_candidate(
        roots, install_plan, abort_plan, candidate, require_released_lock=True
    )
    if ready_binding is None:
        ready_binding = _active_abort_lock_binding(
            roots, install_plan, prefix="abort_ready"
        )
    if set(ready_binding) != {
        "abort_ready_lock_epoch",
        "abort_ready_lock_claim",
        "abort_ready_lock_claim_hash",
        "abort_ready_lock_release",
        "abort_ready_lock_release_hash",
    }:
        raise BootstrapError("pre-config abort ready-lock binding is invalid")
    result: dict[str, Any] = {
        "schema": ABORT_RESULT_SCHEMA,
        "status": "aborted_ready",
        "classification": "static_probe",
        "run_id": roots.run_id,
        "abort_id": abort_plan["abort_id"],
        "abort_plan_hash": abort_plan["plan_hash"],
        "install_transaction_id": candidate["install_transaction_id"],
        "install_plan_hash": candidate["install_plan_hash"],
        "install_result_sha256": candidate["install_result_sha256"],
        "install_result_hash": candidate["install_result_hash"],
        "environment_manifest_sha256": candidate["environment_manifest_sha256"],
        "release_dll_sha256": candidate["release_dll_sha256"],
        "abort_commit_entry_hash": candidate["abort_commit_entry_hash"],
        "final_state_hash": candidate["final_state_hash"],
        "abort_stage_sha256": candidate["abort_stage_sha256"],
        "abort_stage_hash": candidate["abort_stage_hash"],
        "producer": candidate["producer"],
        "abort_lock_epoch": candidate["abort_lock_epoch"],
        "abort_lock_claim": candidate["abort_lock_claim"],
        "abort_lock_claim_hash": candidate["abort_lock_claim_hash"],
        "abort_lock_release": candidate["abort_lock_release"],
        "abort_lock_release_hash": candidate["abort_lock_release_hash"],
        **dict(ready_binding),
        "candidate_sha256": candidate_state["sha256"],
        "candidate_hash": candidate["candidate_hash"],
        "full_environment_restored": True,
        "loader_pair_restored": True,
        "clone_manifest_preserved": True,
        "physical_restore_complete": True,
        "shared_lock_released": True,
        "game_launched": False,
    }
    result["result_hash"] = canonical_hash(result)
    return result


def _validate_abort_result(
    roots: Roots,
    install_plan: Mapping[str, Any],
    abort_plan: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    require_ready_released: bool,
) -> None:
    candidate, candidate_state = stable_json(
        roots.transaction_dir / "abort" / "result-candidate.json",
        "pre-config abort result candidate",
    )
    binding = {
        "abort_ready_lock_epoch": result.get("abort_ready_lock_epoch"),
        "abort_ready_lock_claim": result.get("abort_ready_lock_claim"),
        "abort_ready_lock_claim_hash": result.get("abort_ready_lock_claim_hash"),
        "abort_ready_lock_release": result.get("abort_ready_lock_release"),
        "abort_ready_lock_release_hash": result.get("abort_ready_lock_release_hash"),
    }
    expected = _abort_result_document(
        roots,
        install_plan,
        abort_plan,
        candidate,
        candidate_state,
        ready_binding=binding,
    )
    if not exact_json_equal(dict(result), expected):
        raise BootstrapError("pre-config abort result is not exact")
    if require_ready_released:
        _validate_certificate_lock_release(
            roots,
            install_plan,
            result,
            prefix="abort_ready",
            purpose="abort",
        )
    elif not exact_json_equal(
        _active_abort_lock_binding(roots, install_plan, prefix="abort_ready"),
        binding,
    ):
        raise BootstrapError("pre-config abort result is outside its publication lock")


def _publish_abort_ready_result(
    args: argparse.Namespace,
    roots: Roots,
    manifest: Mapping[str, Any],
    install_plan: Mapping[str, Any],
    install_result: Mapping[str, Any],
    install_result_state: Mapping[str, Any],
    abort_plan: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
    hook: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    abort_root = roots.transaction_dir / "abort"
    candidate_path = abort_root / "result-candidate.json"
    result_path = abort_root / "result.json"
    with exclusive_environment_lock(
        args, roots, install_plan, purpose="abort", hook=hook
    ):
        _abort_external_guard(
            args,
            roots,
            manifest,
            install_plan,
            install_result,
            install_result_state,
            abort_plan,
            explicit,
        )
        candidate, candidate_state = stable_json(
            candidate_path, "pre-config abort result candidate"
        )
        _validate_abort_candidate(
            roots,
            install_plan,
            abort_plan,
            candidate,
            require_released_lock=True,
        )
        final_state = _abort_final_state(
            args, roots, manifest, install_plan, abort_plan, explicit
        )
        if canonical_hash(final_state) != candidate["final_state_hash"]:
            raise BootstrapError("pre-config abort final state changed before ready publication")
        result = _abort_result_document(
            roots,
            install_plan,
            abort_plan,
            candidate,
            candidate_state,
        )
        write_json_create_only(result_path, result, "pre-config abort ready result")
        checkpoint(args, "after_abort_ready_result_publish", hook)
        published, _ = stable_json(result_path, "pre-config abort ready result")
        _validate_abort_result(
            roots,
            install_plan,
            abort_plan,
            published,
            require_ready_released=False,
        )
        if canonical_hash(
            _abort_final_state(
                args, roots, manifest, install_plan, abort_plan, explicit
            )
        ) != candidate["final_state_hash"]:
            raise BootstrapError("pre-config abort final state changed after ready publication")
        checkpoint(args, "before_abort_ready_lock_release", hook)
    checkpoint(args, "after_abort_ready_lock_release", hook)
    published, _ = stable_json(result_path, "pre-config abort ready result")
    _validate_abort_result(
        roots,
        install_plan,
        abort_plan,
        published,
        require_ready_released=True,
    )
    if os.path.lexists(roots.environment_dir / ".finalize.lock"):
        raise BootstrapError("pre-config abort ready result retained a live shared lock")
    final_state = _abort_final_state(
        args, roots, manifest, install_plan, abort_plan, explicit
    )
    if canonical_hash(final_state) != published["final_state_hash"]:
        raise BootstrapError("pre-config abort final state changed after ready-lock release")
    return published


def _abort_latch_present(roots: Roots) -> bool:
    path = roots.transaction_dir / "abort" / "outcome-unknown.json"
    return os.path.lexists(path) or os.path.lexists(path.with_name(path.name + ".new"))


def _abort_mutation_started(
    roots: Roots,
    plan: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
) -> bool:
    abort_root = roots.transaction_dir / "abort"
    try:
        entries = _verify_abort_journal(abort_root, plan)
        if any(
            entry.get("event") != "abort_initialized"
            and not str(entry.get("event") or "").endswith("_claim_prepared")
            for entry in entries
        ):
            return True
    except (BootstrapError, OSError):
        # A malformed journal alone is not proof of an ONI-root mutation.
        pass
    try:
        roles = _abort_role_paths(roots, plan, explicit)
        for step in plan.get("steps", []):
            if step.get("operation") == "set_mtime":
                before_owner = _abort_mtime_expected_owner(
                    abort_root, plan, step
                )
                path = roles.get(str(step.get("role")))
                if (
                    before_owner is not None
                    and path is not None
                    and environment_core.transaction_node_matches(
                        path,
                        _abort_owner_with_mtime(
                            before_owner, int(step["value"])
                        ),
                    )
                ):
                    return True
                continue
            if step.get("operation") != "move":
                continue
            source = roles.get(str(step.get("source_role")))
            target = roles.get(str(step.get("target_role")))
            owner = step.get("owner")
            source_missing = source is not None and not os.path.lexists(source)
            if (
                source_missing
                and step.get("source_role") == "relocated_old_bridge_archive"
                and isinstance(owner, Mapping)
                and _abort_owner_matches(roles.get("old_bridge_archive", pathlib.Path()), owner)
            ):
                source_missing = False
            if (
                source_missing
            ) or (
                target is not None
                and isinstance(owner, Mapping)
                and _abort_owner_matches(target, owner)
            ):
                return True
    except (BootstrapError, OSError, KeyError, TypeError):
        pass
    return False


def _abort_environment_lock_outcome_unknown_present(
    roots: Roots,
    install_plan: Mapping[str, Any],
) -> bool:
    """Detect a non-recoverable abort lock-attempt prefix without adopting it.

    A pre-live lock attempt is evidence mutation rather than an ONI mutation,
    so ``_abort_mutation_started`` intentionally does not count it.  Once an
    externally anchored abort Apply has created an ownerless/claimless,
    partial, swapped, or structurally invalid attempt, however, replay must be
    blocked by the same persistent abort latch.  Fully released historical
    install/replay attempts remain the normal baseline.
    """

    attempt_root = roots.transaction_dir / "environment-lock-attempts"
    try:
        attempts = _lock_attempt_directories(attempt_root, install_plan)
        for index, attempt in enumerate(attempts):
            state = _load_lock_attempt(roots, install_plan, attempt)
            if state.get("release_complete") is True:
                continue
            if index < len(attempts) - 1:
                return True

            # A latest, strictly parsed epoch may be intentionally left live
            # when a transient guard (for example a process starting in the
            # final pre-rename window) rejects before any abort runtime move.
            # Preserve its exact custody for same-epoch retry instead of
            # converting every such refusal into manual outcome_unknown.
            # Malformed owner/claim/pathsets have already raised above.
            live = roots.environment_dir / ".finalize.lock"
            if state.get("released") is True:
                return os.path.lexists(live)
            claim = state.get("claim")
            if not isinstance(claim, Mapping):
                return os.path.lexists(live)
            expected_identity = claim.get("lock_identity")
            payload = state.get("payload")
            if not _valid_identity_list(expected_identity) or not isinstance(
                payload, bytes
            ):
                return True
            stage = attempt / "lock.stage"
            stage_partial = attempt / "lock.stage.new"
            locations = [
                path
                for path in (stage, stage_partial, live)
                if os.path.lexists(path)
            ]
            if len(locations) > 1:
                return True
            if locations:
                location_state = stable_file(
                    locations[0], max_bytes=MAX_JSON_BYTES, collect=True
                )
                location_bytes = location_state.get("bytes")
                if (
                    location_state.get("identity") != expected_identity
                    or not isinstance(location_bytes, bytes)
                    or (
                        locations[0] == stage_partial
                        and not payload.startswith(location_bytes)
                    )
                    or (
                        locations[0] != stage_partial
                        and location_bytes != payload
                    )
                ):
                    return True
            if os.path.lexists(attempt / "acquired.json") and not os.path.lexists(
                live
            ):
                return True
        return False
    except (BootstrapError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return True


def _abort_role_location(path: pathlib.Path) -> dict[str, Any]:
    try:
        if not os.path.lexists(path):
            return {"status": "absent"}
        metadata = os.lstat(path)
        if is_reparse_metadata(metadata):
            return {"status": "unsafe_reparse"}
        if stat.S_ISREG(metadata.st_mode):
            state = stable_file(path, max_bytes=MAX_ARTIFACT_BYTES, collect=False)
            return {
                "status": "file",
                "identity": state["identity"],
                "length": state["length"],
                "sha256": state["sha256"],
            }
        if stat.S_ISDIR(metadata.st_mode):
            snapshot, identity = capture_bound_tree(path)
            return {
                "status": "tree",
                "root_identity": identity["root_identity"],
                "content_hash": canonical_hash(tree_content(snapshot)),
                "identity_hash": canonical_hash(identity),
            }
        return {"status": "unsafe_other"}
    except (BootstrapError, OSError):
        return {"status": "unreadable_or_changed"}


def _publish_abort_outcome_unknown(
    args: argparse.Namespace,
    roots: Roots,
    plan: Mapping[str, Any],
    explicit: Mapping[str, pathlib.Path],
    error: BaseException,
) -> None:
    abort_root = roots.transaction_dir / "abort"
    stage, stage_state = stable_json(
        roots.environment_dir / "runtime-bootstrap-abort-stage.json",
        "pre-config abort stage authorization",
    )
    try:
        entries = _verify_abort_journal(abort_root, plan)
        journal = {
            "entry_count": len(entries),
            "tip_hash": entries[-1]["entry_hash"] if entries else None,
            "status": "verified",
        }
    except (BootstrapError, OSError):
        journal = {"entry_count": None, "tip_hash": None, "status": "unverified"}
    roles = _abort_role_paths(roots, plan, explicit)
    role_locations = {
        role: {
            "path_id_lower": path_id(path),
            "state": _abort_role_location(path),
        }
        for role, path in sorted(roles.items())
    }
    latch: dict[str, Any] = {
        "schema": "oni.live-runtime-bootstrap-abort-outcome-unknown.v1",
        "status": "outcome_unknown",
        "classification": "infra_fail",
        "run_id": roots.run_id,
        "abort_id": plan["abort_id"],
        "abort_plan_hash": plan["plan_hash"],
        "abort_stage_sha256": stage_state["sha256"],
        "abort_stage_hash": stage["stage_hash"],
        "journal": journal,
        "role_locations": role_locations,
        "failure_type": type(error).__name__,
        "failure_hash": sha256_bytes(str(error).encode("utf-8", errors="replace")),
        "manual_recovery_required": True,
        "game_launched": False,
    }
    latch["latch_hash"] = canonical_hash(latch)
    write_json_create_only(
        abort_root / "outcome-unknown.json",
        latch,
        "pre-config abort outcome-unknown latch",
    )


def _execute_abort_inner(
    args: argparse.Namespace,
    *,
    hook: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    roots = resolve_roots(args)
    if _abort_latch_present(roots):
        raise BootstrapError(
            "pre-config abort outcome_unknown latch requires manual recovery"
        )
    if sum(
        bool(value)
        for value in (args.stage, args.recover_stage, args.apply, args.what_if)
    ) != 1:
        raise BootstrapError(
            "select exactly one of --stage, --recover-stage, --apply, or --what-if"
        )
    if args.test_fail_at and not roots.test_root:
        raise BootstrapError("fault injection is TestRoot-only")
    require_processes_stopped(args, roots)
    manifest, environment_binding, _journal = load_environment(args, roots)
    install_plan, _ = stable_json(
        roots.transaction_dir / "plan.json", "runtime bootstrap install plan"
    )
    install_intent, _ = stable_json(
        roots.transaction_dir / "intent.json", "runtime bootstrap install intent"
    )
    releases, _release_payloads = release_artifacts(args, roots)
    validate_deterministic_contract(
        args,
        roots,
        manifest,
        environment_binding,
        releases,
        install_intent,
        install_plan,
    )
    install_result, install_result_state = _load_install_ready_for_abort(
        args, roots, install_plan
    )
    explicit = _abort_explicit_paths(args, roots, manifest)
    abort_stage: Optional[dict[str, Any]] = None
    abort_stage_state: Optional[dict[str, Any]] = None
    if args.apply:
        abort_stage, abort_stage_state, abort_intent, abort_plan = _load_abort_stage(
            roots,
            expected_file_sha256=getattr(args, "expected_stage_sha256", None),
        )
    else:
        _abort_prohibited_evidence_absent(
            roots, manifest, install_plan, explicit
        )
        abort_intent, abort_plan = _capture_abort_contract(
            args,
            roots,
            manifest,
            install_plan,
            install_result,
            install_result_state,
            explicit,
        )
    _validate_abort_plan_shape(
        roots,
        abort_plan,
        install_plan,
        install_result,
        install_result_state,
        explicit,
    )
    _validate_abort_intent(args, roots, abort_intent, abort_plan)
    validate_legacy_path_budget(roots, install_plan, abort_plan, explicit)
    expected_abort_id = getattr(args, "expected_abort_id", None)
    nonce = getattr(args, "stage_nonce", None)
    if args.what_if:
        if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
            raise BootstrapError("abort WhatIf requires a caller-frozen 32-hex stage nonce")
        expected_stage = _abort_stage_document(
            roots, abort_intent, abort_plan, nonce
        )
        return {
            "schema": "oni.live-runtime-bootstrap-abort-what-if.v1",
            "status": "would_abort_pre_config",
            "classification": "static_probe",
            "run_id": roots.run_id,
            "abort_id": abort_plan["abort_id"],
            "abort_plan_hash": abort_plan["plan_hash"],
            "install_result_sha256": install_result_state["sha256"],
            "stage_nonce": nonce,
            "expected_stage_sha256": sha256_bytes(document_bytes(expected_stage)),
            "expected_stage_hash": expected_stage["stage_hash"],
            "physical_step_count": len(abort_plan["steps"]),
            "write_count": 0,
            "game_launched": False,
        }
    if expected_abort_id != abort_plan["abort_id"]:
        raise BootstrapError("abort stage/apply requires the WhatIf-returned abort id")
    if args.stage or args.recover_stage:
        if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
            raise BootstrapError("abort stage requires a caller-frozen 32-hex nonce")
        document = _abort_stage_document(roots, abort_intent, abort_plan, nonce)
        expected_raw = sha256_bytes(document_bytes(document))
        if require_hash(
            getattr(args, "expected_stage_sha256", None),
            "external expected abort stage raw hash",
        ) != expected_raw:
            raise BootstrapError("abort stage expected hash does not match its document")
        if any(
            os.path.lexists(roots.transaction_dir / "abort" / name)
            for name in ("owner.json", "intent.json", "plan.json")
        ):
            raise BootstrapError("abort stage cannot run after abort transaction initialization")
        stage_path = roots.environment_dir / "runtime-bootstrap-abort-stage.json"
        if args.stage and (
            os.path.lexists(stage_path)
            or os.path.lexists(stage_path.with_name(stage_path.name + ".new"))
        ):
            raise BootstrapError(
                "abort stage first publication requires both stage names to be absent; "
                "use --recover-stage only after a caller-observed uncertain stage attempt"
            )
        write_json_create_only(stage_path, document, "pre-config abort stage authorization")
        checkpoint(args, "after_abort_stage_publish", hook)
        loaded, state, _intent, _plan = _load_abort_stage(
            roots, expected_file_sha256=expected_raw
        )
        return {
            "schema": "oni.live-runtime-bootstrap-abort-stage-result.v1",
            "status": "abort_stage_recovered" if args.recover_stage else "abort_staged",
            "classification": "static_probe",
            "run_id": roots.run_id,
            "abort_id": abort_plan["abort_id"],
            "abort_plan_hash": abort_plan["plan_hash"],
            "stage_nonce": nonce,
            "stage_sha256": state["sha256"],
            "stage_hash": loaded["stage_hash"],
            "runtime_write_count": 0,
            "game_launched": False,
        }
    assert abort_stage is not None and abort_stage_state is not None
    abort_root = roots.transaction_dir / "abort"
    _initialize_abort_transaction(
        args,
        roots,
        abort_intent,
        abort_plan,
        abort_stage,
        abort_stage_state,
        hook,
    )
    roles = _abort_role_paths(roots, abort_plan, explicit)
    with transaction_lock(roots.transaction_dir):
        result_path = abort_root / "result.json"
        candidate_path = abort_root / "result-candidate.json"
        if os.path.lexists(result_path):
            result_before, _ = stable_json(result_path, "pre-config abort ready result")
            phase = _certificate_lock_phase(
                roots,
                install_plan,
                result_before,
                prefix="abort_ready",
                purpose="abort",
            )
            with exclusive_environment_lock(
                args, roots, install_plan, purpose="abort", hook=hook
            ):
                _abort_external_guard(
                    args,
                    roots,
                    manifest,
                    install_plan,
                    install_result,
                    install_result_state,
                    abort_plan,
                    explicit,
                )
                result, _ = stable_json(result_path, "pre-config abort ready result")
                _validate_abort_result(
                    roots,
                    install_plan,
                    abort_plan,
                    result,
                    require_ready_released=(phase != "active"),
                )
                final_state = _abort_final_state(
                    args, roots, manifest, install_plan, abort_plan, explicit
                )
                if canonical_hash(final_state) != result["final_state_hash"]:
                    raise BootstrapError("pre-config abort replay final state changed")
                checkpoint(args, "before_abort_replay_lock_release", hook)
            result, _ = stable_json(result_path, "pre-config abort ready result")
            _validate_abort_result(
                roots,
                install_plan,
                abort_plan,
                result,
                require_ready_released=True,
            )
            return {
                "schema": "oni.live-runtime-bootstrap-abort-replay.v1",
                "status": "already_aborted",
                "classification": "static_probe",
                "run_id": roots.run_id,
                "abort_id": abort_plan["abort_id"],
                "result_hash": result["result_hash"],
                "runtime_write_count": 0,
                "evidence_epoch_written": True,
                "game_launched": False,
            }
        if os.path.lexists(candidate_path):
            candidate_before, _ = stable_json(
                candidate_path, "pre-config abort result candidate"
            )
            phase = _certificate_lock_phase(
                roots,
                install_plan,
                candidate_before,
                prefix="abort",
                purpose="abort",
            )
            with exclusive_environment_lock(
                args, roots, install_plan, purpose="abort", hook=hook
            ):
                _abort_external_guard(
                    args,
                    roots,
                    manifest,
                    install_plan,
                    install_result,
                    install_result_state,
                    abort_plan,
                    explicit,
                )
                candidate, _ = stable_json(
                    candidate_path, "pre-config abort result candidate"
                )
                _validate_abort_candidate(
                    roots,
                    install_plan,
                    abort_plan,
                    candidate,
                    require_released_lock=(phase != "active"),
                )
                final_state = _abort_final_state(
                    args, roots, manifest, install_plan, abort_plan, explicit
                )
                if canonical_hash(final_state) != candidate["final_state_hash"]:
                    raise BootstrapError("pre-config abort candidate recovery state changed")
                checkpoint(args, "before_abort_candidate_lock_release", hook)
            return _publish_abort_ready_result(
                args,
                roots,
                manifest,
                install_plan,
                install_result,
                install_result_state,
                abort_plan,
                explicit,
                hook,
            )
        with exclusive_environment_lock(
            args, roots, install_plan, purpose="abort", hook=hook
        ):
            completed_step_events = _abort_completed_prefix_guard(
                roots, abort_root, abort_plan, roles
            )
            for step in abort_plan["steps"]:
                if str(step["event"]) in completed_step_events:
                    continue
                _abort_external_guard(
                    args,
                    roots,
                    manifest,
                    install_plan,
                    install_result,
                    install_result_state,
                    abort_plan,
                    explicit,
                )
                _execute_abort_step(
                    args, roots, abort_root, abort_plan, step, roles, hook
                )
            final_state = _abort_final_state(
                args, roots, manifest, install_plan, abort_plan, explicit
            )
            commit = _append_abort_journal(
                abort_root,
                abort_plan,
                "abort_commit_verified",
                {
                    "final_state_hash": canonical_hash(final_state),
                    "step_count": len(abort_plan["steps"]),
                    "full_environment_restored": True,
                },
            )
            checkpoint(args, "after_abort_commit_verified_journal", hook)
            if commit.get("data", {}).get("final_state_hash") != canonical_hash(
                _abort_final_state(
                    args, roots, manifest, install_plan, abort_plan, explicit
                )
            ):
                raise BootstrapError("pre-config abort final state changed after commit")
            candidate = _abort_candidate_document(
                roots, install_plan, abort_plan, final_state
            )
            write_json_create_only(
                candidate_path, candidate, "pre-config abort result candidate"
            )
            checkpoint(args, "after_abort_result_candidate_publish", hook)
            published_candidate, _ = stable_json(
                candidate_path, "pre-config abort result candidate"
            )
            _validate_abort_candidate(
                roots,
                install_plan,
                abort_plan,
                published_candidate,
                require_released_lock=False,
            )
        checkpoint(args, "after_abort_physical_lock_release", hook)
        return _publish_abort_ready_result(
            args,
            roots,
            manifest,
            install_plan,
            install_result,
            install_result_state,
            abort_plan,
            explicit,
            hook,
        )


def execute_abort(
    args: argparse.Namespace,
    *,
    hook: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    try:
        return _execute_abort_inner(args, hook=hook)
    except InjectedCrash:
        # A simulated power loss must exercise ordinary deterministic recovery,
        # not be converted into a manual external-interference latch.
        raise
    except (BootstrapError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, CooperativeLockBusyError):
            # The shared name was occupied before this abort epoch published or
            # touched any live inode.  The immutable abort state remains safe
            # to retry after the cooperating owner releases its lock.
            raise
        try:
            roots = resolve_roots(args)
            stage_path = roots.environment_dir / "runtime-bootstrap-abort-stage.json"
            if os.path.lexists(stage_path):
                stage, _ = stable_json(stage_path, "pre-config abort stage authorization")
                plan_value = stage.get("abort_plan")
                if isinstance(plan_value, Mapping):
                    plan = dict(plan_value)
                    install_plan_value, _install_plan_state = stable_json(
                        roots.transaction_dir / "plan.json",
                        "runtime bootstrap install plan for abort failure classification",
                    )
                    validate_plan_shape(install_plan_value)
                    if not exact_json_equal(
                        install_plan_value.get("plan_hash"),
                        plan.get("install_plan_hash"),
                    ):
                        raise BootstrapError(
                            "abort failure classification install plan does not match "
                            "the externally anchored abort plan"
                        )
                    explicit = {
                        "pre_boundary": normalized(args.pre_boundary),
                        "clone_manifest": normalized(args.clone_manifest),
                        "source_save": normalized(args.source_save),
                        "loader_save": normalized(args.loader_save),
                    }
                    explicit["source_preview"] = explicit["source_save"].with_suffix(".png")
                    explicit["loader_preview"] = explicit["loader_save"].with_suffix(".png")
                    if not _abort_latch_present(roots) and (
                        _abort_mutation_started(roots, plan, explicit)
                        or _abort_environment_lock_outcome_unknown_present(
                            roots, install_plan_value
                        )
                    ):
                        _publish_abort_outcome_unknown(
                            args, roots, plan, explicit, exc
                        )
        except (BootstrapError, OSError, ValueError, TypeError, json.JSONDecodeError):
            # Preserve the original refusal.  A failed latch publication never
            # authorizes replay and any partial latch name is rejected first on
            # the next invocation.
            pass
        raise


def stage_install_transaction(
    args: argparse.Namespace,
    roots: Roots,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
    hook: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    """Create the externally anchored, workspace-only install transaction."""

    nonce = getattr(args, "stage_nonce", None)
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise BootstrapError("stage requires the caller-frozen 32-hex nonce")
    stage_document = _initialization_intent(roots, intent, plan, nonce)
    expected_raw = sha256_bytes(document_bytes(stage_document))
    if require_hash(
        getattr(args, "expected_stage_sha256", None),
        "external expected stage raw hash",
    ) != expected_raw:
        raise BootstrapError("stage expected raw hash does not match the nonce-bound document")
    expected_transaction_id = getattr(args, "expected_transaction_id", None)
    if expected_transaction_id != plan["transaction_id"]:
        raise BootstrapError("stage requires the WhatIf-returned transaction id")
    if os.path.lexists(roots.transaction_dir):
        raise BootstrapError("stage cannot run after support transaction initialization")
    stage_path = roots.environment_dir / "runtime-bootstrap-install-stage.json"
    if args.stage and (
        os.path.lexists(stage_path)
        or os.path.lexists(stage_path.with_name(stage_path.name + ".new"))
    ):
        raise BootstrapError(
            "install stage first publication requires both stage names to be absent; "
            "use --recover-stage only after a caller-observed uncertain stage attempt"
        )
    write_json_create_only(stage_path, stage_document, "runtime bootstrap stage authorization")
    checkpoint(args, "after_install_stage_publish", hook)
    loaded, stage_state = _load_initialization_intent(
        roots, intent, plan, expected_file_sha256=expected_raw
    )
    # Staging publishes one evidence-side authorization file only.  Support
    # directory initialization is intentionally deferred to externally
    # authorized apply.
    return {
        "schema": "oni.live-runtime-bootstrap-stage-result.v1",
        "status": "stage_recovered" if args.recover_stage else "staged",
        "classification": "static_probe",
        "run_id": roots.run_id,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "stage_nonce": nonce,
        "stage_sha256": stage_state["sha256"],
        "stage_hash": loaded["stage_hash"],
        "runtime_write_count": 0,
        "game_launched": False,
    }


def _install_latch_present(roots: Roots) -> bool:
    path = roots.transaction_dir / "outcome-unknown.json"
    return os.path.lexists(path) or os.path.lexists(path.with_name(path.name + ".new"))


def _publish_install_outcome_unknown(
    roots: Roots,
    plan: Mapping[str, Any],
    error: BaseException,
) -> None:
    stage, stage_state = stable_json(
        roots.environment_dir / "runtime-bootstrap-install-stage.json",
        "runtime bootstrap install stage authorization",
    )
    try:
        entries = verify_journal(roots.transaction_dir)
        journal = {
            "status": "verified",
            "entry_count": len(entries),
            "tip_hash": entries[-1]["entry_hash"] if entries else None,
        }
    except (BootstrapError, OSError):
        journal = {"status": "unverified", "entry_count": None, "tip_hash": None}
    archive_relative = plan.get("desired", {}).get("archive_relative")
    locations = {
        "live_probe": roots.oni_root / pathlib.Path(*PROBE_RELATIVE.parts),
        "live_bridge": roots.oni_root / pathlib.Path(*BRIDGE_RELATIVE.parts),
        "archive_root": roots.oni_root / pathlib.Path(*ARCHIVE_ROOT_RELATIVE.parts),
        "old_probe_recovery": roots.transaction_dir / "recovery" / "old-probe",
        "probe_stage": roots.transaction_dir / "stage" / "probe",
        "bridge_stage": roots.transaction_dir / "stage" / "bridge",
    }
    if isinstance(archive_relative, str) and archive_relative:
        locations["old_bridge_archive"] = roots.oni_root / pathlib.Path(
            *pathlib.PurePosixPath(archive_relative).parts
        )
    latch: dict[str, Any] = {
        "schema": "oni.live-runtime-bootstrap-install-outcome-unknown.v1",
        "status": "outcome_unknown",
        "classification": "infra_fail",
        "run_id": roots.run_id,
        "transaction_id": plan["transaction_id"],
        "plan_hash": plan["plan_hash"],
        "install_stage_sha256": stage_state["sha256"],
        "install_stage_hash": stage["stage_hash"],
        "journal": journal,
        "role_locations": {
            role: {
                "path_id_lower": path_id(path),
                "state": _abort_role_location(path),
            }
            for role, path in sorted(locations.items())
        },
        "failure_type": type(error).__name__,
        "failure_hash": sha256_bytes(str(error).encode("utf-8", errors="replace")),
        "manual_recovery_required": True,
        "game_launched": False,
    }
    latch["latch_hash"] = canonical_hash(latch)
    write_json_create_only(
        roots.transaction_dir / "outcome-unknown.json",
        latch,
        "runtime bootstrap install outcome-unknown latch",
    )


def _execute_install_inner(
    args: argparse.Namespace,
    *,
    hook: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    roots = resolve_roots(args)
    if _initialization_latch_present(roots):
        raise BootstrapError(
            "runtime bootstrap initialization outcome_unknown latch requires manual recovery"
        )
    if _install_latch_present(roots):
        raise BootstrapError(
            "runtime bootstrap install outcome_unknown latch requires manual recovery"
        )
    if sum(
        bool(value)
        for value in (args.stage, args.recover_stage, args.apply, args.what_if)
    ) != 1:
        raise BootstrapError(
            "select exactly one of --stage, --recover-stage, --apply, or --what-if"
        )
    if args.test_fail_at and not roots.test_root:
        raise BootstrapError("fault injection is TestRoot-only")
    require_processes_stopped(args, roots)
    manifest, environment_binding, _journal = load_environment(args, roots)
    transaction_exists = os.path.lexists(roots.transaction_dir)
    published_plan_path = roots.transaction_dir / "plan.json"
    stage_authorization: Optional[dict[str, Any]] = None
    if args.apply:
        stage_path = roots.environment_dir / "runtime-bootstrap-install-stage.json"
        stage_authorization, stage_state = stable_json(
            stage_path, "runtime bootstrap install stage authorization"
        )
        expected_stage_sha256 = require_hash(
            getattr(args, "expected_stage_sha256", None),
            "external install stage raw hash",
        )
        if stage_state.get("sha256") != expected_stage_sha256:
            raise BootstrapError("install stage authorization does not match its external hash")
        intent_value = stage_authorization.get("install_intent")
        plan_value = stage_authorization.get("install_plan")
        if not isinstance(intent_value, Mapping) or not isinstance(plan_value, Mapping):
            raise BootstrapError("install stage authorization bundle is incomplete")
        intent, plan = dict(intent_value), dict(plan_value)
        loaded_stage, _ = _load_initialization_intent(
            roots,
            intent,
            plan,
            expected_file_sha256=expected_stage_sha256,
        )
        if not exact_json_equal(stage_authorization, loaded_stage):
            raise BootstrapError("install stage authorization changed during load")
        if getattr(args, "expected_transaction_id", None) != plan.get("transaction_id"):
            raise BootstrapError("apply requires the exact WhatIf-returned transaction id")
        if os.path.lexists(published_plan_path):
            published_plan, _ = stable_json(published_plan_path, "runtime bootstrap plan")
            published_intent, _ = stable_json(
                roots.transaction_dir / "intent.json", "runtime bootstrap intent"
            )
            if not exact_json_equal(published_plan, plan) or not exact_json_equal(
                published_intent, intent
            ):
                raise BootstrapError("published support transaction conflicts with stage authorization")
        # An unclaimed support-tree prefix is initialization residue, not a
        # runtime commit.  Avoid asking the strict journal reducer to read an
        # empty/unclaimed journal directory before initialize_transaction can
        # classify it and publish the permanent initialization latch.
        commit_started = (
            transaction_exists
            and os.path.lexists(roots.transaction_dir / "owner.json")
            and os.path.lexists(published_plan_path)
            and physical_commit_started(roots, plan)
        )
        try:
            current_releases, current_payloads = release_artifacts(args, roots)
        except BootstrapError:
            if not commit_started:
                raise
            current_releases, current_payloads = {}, {}
        expected_releases = plan.get("inputs", {}).get("release_artifacts")
        if exact_json_equal(current_releases, expected_releases):
            releases, payloads = current_releases, current_payloads
        elif commit_started:
            if not isinstance(expected_releases, Mapping):
                raise BootstrapError("frozen release-artifact plan is invalid")
            releases = dict(expected_releases)
            payloads = frozen_probe_payloads(roots, plan)
        else:
            raise BootstrapError("workspace artifacts changed before physical commit")
    elif transaction_exists and os.path.lexists(published_plan_path):
        plan, _ = stable_json(published_plan_path, "runtime bootstrap plan")
        intent, _ = stable_json(roots.transaction_dir / "intent.json", "runtime bootstrap intent")
        validate_plan_shape(plan)
        validate_intent_shape(intent, plan)
        if (
            intent.get("run_id") != roots.run_id
            or intent.get("confirm_live_run_id") != args.confirm_live_run_id
            or intent.get("expected_mod_version") != args.expected_mod_version
            or intent.get("environment_manifest_sha256")
            != args.expected_environment_manifest_sha256
            or intent.get("release_dll_sha256") != args.expected_release_dll_sha256
        ):
            raise BootstrapError("runtime bootstrap replay invocation does not match its latch")
        if not exact_json_equal(plan.get("inputs", {}).get("environment_manifest"), environment_binding):
            raise BootstrapError("environment manifest identity changed after bootstrap latch")
        commit_started = physical_commit_started(roots, plan)
        try:
            current_releases, current_payloads = release_artifacts(args, roots)
        except BootstrapError:
            if not commit_started:
                raise
            current_releases, current_payloads = {}, {}
        expected_releases = plan.get("inputs", {}).get("release_artifacts")
        if exact_json_equal(current_releases, expected_releases):
            releases = current_releases
            payloads = current_payloads
        elif commit_started:
            if not isinstance(expected_releases, Mapping):
                raise BootstrapError("frozen release-artifact plan is invalid")
            releases = dict(expected_releases)
            payloads = frozen_probe_payloads(roots, plan)
        else:
            raise BootstrapError("workspace artifacts changed before physical commit")
    else:
        releases, payloads = release_artifacts(args, roots)
        intent, plan = capture_initial_plan(
            args,
            roots,
            manifest,
            environment_binding,
            releases,
        )
    validate_deterministic_contract(
        args,
        roots,
        manifest,
        environment_binding,
        releases,
        intent,
        plan,
    )
    validate_legacy_path_budget(roots, plan)
    if args.what_if:
        nonce = getattr(args, "stage_nonce", None)
        if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
            raise BootstrapError("WhatIf requires a caller-frozen 32-hex stage nonce")
        expected_stage = _initialization_intent(roots, intent, plan, nonce)
        return {
            "schema": "oni.live-runtime-bootstrap-what-if.v1",
            "status": "would_install" if not transaction_exists else "would_resume_or_refuse_replay",
            "classification": "static_probe",
            "run_id": roots.run_id,
            "transaction_id": plan["transaction_id"],
            "plan_hash": plan["plan_hash"],
            "environment_manifest_sha256": environment_binding["sha256"],
            "release_dll_sha256": releases[ARTIFACT_NAMES[0]]["sha256"],
            "release_artifacts": {
                name: {
                    "length": releases[name]["length"],
                    "sha256": releases[name]["sha256"],
                }
                for name in sorted(ARTIFACT_NAMES, key=str.casefold)
            },
            "workspace_artifact_count": len(ARTIFACT_NAMES),
            "physical_step_count": len(plan["physical_steps"]),
            "stage_nonce": nonce,
            "expected_stage_sha256": sha256_bytes(document_bytes(expected_stage)),
            "expected_stage_hash": expected_stage["stage_hash"],
            "write_count": 0,
            "game_launched": False,
        }
    if args.stage or args.recover_stage:
        return stage_install_transaction(args, roots, intent, plan, hook)
    assert stage_authorization is not None
    # A partial publication may lack intent/plan/journal only at one of the
    # explicit publication checkpoints. Initialization verifies every
    # existing immutable file before filling the missing suffix.
    initialize_transaction(args, roots, intent, plan, stage_authorization, hook)
    return apply_transaction(args, roots, plan, payloads, hook)


def execute_install(
    args: argparse.Namespace,
    *,
    hook: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    try:
        return _execute_install_inner(args, hook=hook)
    except InjectedCrash:
        raise
    except (BootstrapError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, CooperativeLockBusyError):
            # A cooperative config/finalize/abort lock can interpose between
            # install commit and ready-certificate publication.  Because our
            # epoch has not published a live inode, this is a safe refusal and
            # must not poison the immutable candidate with outcome_unknown.
            raise
        try:
            roots = resolve_roots(args)
            stage_path = roots.environment_dir / "runtime-bootstrap-install-stage.json"
            if args.apply and os.path.lexists(stage_path):
                stage, stage_state = stable_json(
                    stage_path, "runtime bootstrap install stage authorization"
                )
                plan_value = stage.get("install_plan")
                if isinstance(plan_value, Mapping):
                    if not _install_latch_present(
                        roots
                    ) and not _initialization_latch_present(roots):
                        initialization_present = _initialization_artifact_present(roots)
                        origin_complete = _initialization_origin_complete(
                            roots, plan_value
                        )
                        if initialization_present and not origin_complete:
                            # Initialization residue wins classification before
                            # the strict journal reducer.  In particular, an
                            # unclaimed empty journal directory must never make
                            # physical_commit_started raise and silently bypass
                            # the persistent initialization latch.
                            _publish_initialization_outcome_unknown(
                                args,
                                roots,
                                stage,
                                stage_state,
                                plan_value,
                                exc,
                                hook,
                            )
                        elif origin_complete:
                            try:
                                commit_started = physical_commit_started(
                                    roots, plan_value
                                )
                            except (
                                BootstrapError,
                                OSError,
                                ValueError,
                                TypeError,
                                json.JSONDecodeError,
                            ):
                                # Once the immutable origin is complete, a
                                # malformed later journal can no longer be
                                # downgraded to initialization or fail open.
                                _publish_install_outcome_unknown(
                                    roots, plan_value, exc
                                )
                            else:
                                if commit_started:
                                    _publish_install_outcome_unknown(
                                        roots, plan_value, exc
                                    )
        except (BootstrapError, OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        raise


def execute(
    args: argparse.Namespace,
    *,
    hook: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    operation = getattr(args, "operation", "install")
    if operation == "install":
        return execute_install(args, hook=hook)
    if operation == "abort":
        return execute_abort(args, hook=hook)
    raise BootstrapError("runtime bootstrap operation is invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("install", "abort"), default="install")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--oni-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--environment-manifest", required=True)
    parser.add_argument("--expected-environment-manifest-sha256", required=True)
    parser.add_argument("--release-dll", required=True)
    parser.add_argument("--expected-release-dll-sha256", required=True)
    parser.add_argument("--expected-mod-version", required=True)
    parser.add_argument("--confirm-live-run-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage", action="store_true")
    mode.add_argument("--recover-stage", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--what-if", action="store_true")
    parser.add_argument("--stage-nonce")
    parser.add_argument("--expected-stage-sha256")
    parser.add_argument("--expected-transaction-id")
    parser.add_argument("--expected-abort-id")
    parser.add_argument("--expected-install-result-sha256")
    parser.add_argument("--pre-boundary")
    parser.add_argument("--clone-manifest")
    parser.add_argument("--source-save")
    parser.add_argument("--loader-save")
    parser.add_argument("--loader-stem")
    parser.add_argument("--test-root", help=argparse.SUPPRESS)
    parser.add_argument("--process-state-fixture", help=argparse.SUPPRESS)
    parser.add_argument("--test-fail-at", default="", help=argparse.SUPPRESS)
    return parser


def bootstrap_error_document(error: BootstrapError) -> dict[str, Any]:
    """Build the additive v1 refusal document used by the CLI boundary."""

    result: dict[str, Any] = {
        "schema": "oni.live-runtime-bootstrap-error.v1",
        "status": "blocked",
        "classification": "infra_fail",
        "error": str(error),
    }
    if error.diagnostic is not None:
        result["diagnostic"] = error.diagnostic
    return result


def unexpected_local_error_document(error: Exception) -> dict[str, Any]:
    """Build a redacted refusal for uncategorized local I/O/data failures."""

    result: dict[str, Any] = {
        "schema": "oni.live-runtime-bootstrap-error.v1",
        "status": "blocked",
        "classification": "infra_fail",
        "error": "unexpected local I/O or data-shape failure",
    }
    if isinstance(error, OSError):
        result["diagnostic"] = {
            "schema": "oni.local-os-error-diagnostic.v1",
            "operation": "local_io",
            "action_outcome": "unknown",
            "native_error": native_os_error_diagnostic(error),
        }
    return result


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except InjectedCrash as exc:
        print(
            json.dumps(
                {
                    "schema": "oni.live-runtime-bootstrap-injected-crash.v1",
                    "status": "interrupted",
                    "classification": "infra_fail",
                    "checkpoint": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 99
    except BootstrapError as exc:
        print(
            json.dumps(
                bootstrap_error_document(exc),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except OSError as exc:
        print(
            json.dumps(
                unexpected_local_error_document(exc),
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                unexpected_local_error_document(exc),
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
