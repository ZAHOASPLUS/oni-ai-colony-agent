"""Shared custody primitives for the command lifecycle.

The command queue has several independent writers: suggestion synchronization,
operator/runtime preparation, Colony Plan publication, expiry, quarantine, and
approval commit. This module supplies the one durable process boundary they
all share. It intentionally contains no policy or game-runtime logic.

The lifecycle lock is a persistent file. Ownership is an OS byte-range lock
held on an open descriptor, so a crashed process releases ownership without
requiring stale-mtime deletion. A small in-process owner table only serializes
threads and makes recursive acquisition explicit; it never lets a different
thread pass through the lock.
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from agent.field_contracts import NON_MUTATING_ACTIONS


COMMAND_STAGES: Tuple[str, ...] = (
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
DISPOSITION_STAGES = frozenset({"rejected", "quarantine"})
LIFECYCLE_LOCK_NAME = ".mutation-lifecycle.lock"
LIFECYCLE_RECOVERY_DIR_NAME = "lifecycle-recovery"
LIFECYCLE_JOURNAL_SCHEMA = "oni.command-lifecycle-recovery.v2"
COMMAND_SCHEMA = "oni.command.v3"
AUTONOMOUS_COMMAND_SCHEMA = "oni.command.v4"
PENDING_STATUS = "pending_human_approval"
AUTONOMOUS_PENDING_STATUS = "pending_autonomous_authorization"
APPROVED_STATUS = "approved"
DISPOSITION_STATUSES = {"rejected": "rejected", "quarantine": "quarantined"}
_LOCK_HEADER = b"ONI command lifecycle custody v1\n"
_REPARSE_POINT = 0x0400
_MOVEFILE_WRITE_THROUGH = 0x00000008
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_DEFAULT_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.01
_MAX_THREAD_RECURSION = 16
_MAX_JOURNAL_STRING = 512
_MAX_TRANSACTION_STRING = 80
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
# The C# executor does not take the lifecycle lock.  Its durable replace
# writes "<id>.json.tmp-<guid>" and File.Replace leaves "<id>.json.backup-
# <guid>" for a moment (ExecutionClaimStore.TryReplaceFileContents).
_EXECUTOR_TRANSIENT_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.json\.(?:tmp|backup)-[0-9a-f]{32}$"
)
_SCAN_SETTLE_ATTEMPTS = 8
_SCAN_SETTLE_DELAY_SECONDS = 0.05
_TRANSACTION_RE = re.compile(r"^tx-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9A-F]{64}$")
_JOURNAL_PHASES = frozenset({"staged", "published", "restoring", "completing"})
_JOURNAL_KEYS = frozenset(
    {
        "schema",
        "transaction",
        "command_id",
        "operation",
        "source_stage",
        "source_status",
        "target_stage",
        "target_status",
        "source",
        "target",
        "original_sha256",
        "final_sha256",
        "original_identity",
        "original_mtime_ns",
        "original_payload",
        "original_payload_identity",
        "final_payload",
        "final_payload_identity",
        "phase",
        "created_at",
        "journal_identity",
        "journal_sha256",
    }
)


class CommandCustodyError(RuntimeError):
    """Base class for fail-closed lifecycle custody errors."""


class CommandCustodyTimeout(CommandCustodyError):
    """The shared lifecycle lock could not be acquired before the deadline."""


class CommandCustodyConflict(CommandCustodyError):
    """A path, identity, stage, or command-id conflict was observed."""


class CommandCustodyUnknown(CommandCustodyError):
    """A lifecycle transition may have crossed a filesystem boundary."""


@dataclass
class _ThreadOwner:
    condition: threading.Condition
    owner_thread_id: Optional[int] = None
    depth: int = 0
    descriptor: Optional[int] = None
    identity: Optional[Tuple[int, int]] = None
    parent_identity: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class _CommandLocation:
    stage: str
    path: pathlib.Path
    payload: bytes
    identity: Tuple[int, int]
    mtime_ns: int
    command: Dict[str, Any]


@dataclass(frozen=True)
class PendingEnvelopeObservation:
    """Stable, revalidatable observation of one pending command envelope."""

    path: pathlib.Path
    payload: bytes
    identity: Tuple[int, int]
    mtime_ns: int


@dataclass(frozen=True)
class _JournalRecord:
    path: pathlib.Path
    raw: bytes
    identity: Tuple[int, int]
    journal: Dict[str, Any]


_OWNER_REGISTRY: Dict[str, _ThreadOwner] = {}
_OWNER_REGISTRY_GUARD = threading.Lock()
_KNOWN_DIRECTORY_IDENTITIES: Dict[str, Tuple[int, int]] = {}


def _absolute(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(path).expanduser().absolute()


def _same_path(left: pathlib.Path, right: pathlib.Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _identity(metadata: os.stat_result) -> Tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _mtime_ns(metadata: os.stat_result) -> int:
    return int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000)))


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT)


def _bind_directory_identity(
    path: pathlib.Path,
    metadata: os.stat_result,
    *,
    label: str,
) -> Tuple[int, int]:
    """Remember a safe directory name and reject later replacement/recreation."""

    identity = _identity(metadata)
    key = os.path.normcase(os.path.abspath(str(path)))
    with _OWNER_REGISTRY_GUARD:
        previous = _KNOWN_DIRECTORY_IDENTITIES.get(key)
        if previous is not None and previous != identity:
            raise CommandCustodyConflict(f"{label} identity changed: {path}")
        _KNOWN_DIRECTORY_IDENTITIES[key] = identity
    return identity


def _lstat_safe(
    path: pathlib.Path,
    *,
    label: str,
    allow_missing: bool = False,
) -> Optional[os.stat_result]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise CommandCustodyError(f"{label} is missing: {path}")
    except OSError as exc:
        raise CommandCustodyError(f"{label} cannot be inspected: {path}") from exc
    if path.is_symlink() or _is_reparse(metadata):
        raise CommandCustodyConflict(f"{label} is a symlink or reparse point: {path}")
    return metadata


def _ensure_directory(path: pathlib.Path, *, label: str) -> pathlib.Path:
    """Create a directory only after checking every existing ancestor."""

    path = _absolute(path)
    chain: List[pathlib.Path] = []
    current = path
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(chain):
        existing = _lstat_safe(item, label=f"{label} ancestor", allow_missing=True)
        if existing is None:
            with _OWNER_REGISTRY_GUARD:
                previously_bound = (
                    os.path.normcase(os.path.abspath(str(item)))
                    in _KNOWN_DIRECTORY_IDENTITIES
                )
            if previously_bound:
                raise CommandCustodyConflict(f"{label} ancestor was removed or recreated: {item}")
            parent_before = _lstat_safe(item.parent, label=f"{label} parent")
            assert parent_before is not None
            if not stat.S_ISDIR(parent_before.st_mode):
                raise CommandCustodyConflict(f"{label} parent is not a directory: {item.parent}")
            try:
                item.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise CommandCustodyError(f"{label} cannot be created: {item}") from exc
            existing = _lstat_safe(item, label=f"{label} ancestor")
            parent_after = _lstat_safe(item.parent, label=f"{label} parent")
            assert parent_after is not None
            if _identity(parent_before) != _identity(parent_after):
                raise CommandCustodyUnknown(f"{label} parent changed while creating: {item}")
        assert existing is not None
        if not stat.S_ISDIR(existing.st_mode):
            raise CommandCustodyConflict(f"{label} is not a directory: {item}")
        _bind_directory_identity(item, existing, label=f"{label} ancestor")
    return path


def _directory_entries(path: pathlib.Path, *, label: str) -> List[Tuple[pathlib.Path, os.stat_result]]:
    metadata = _lstat_safe(path, label=label)
    assert metadata is not None
    if not stat.S_ISDIR(metadata.st_mode):
        raise CommandCustodyConflict(f"{label} is not a directory: {path}")
    try:
        children = sorted(path.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise CommandCustodyError(f"{label} cannot be enumerated: {path}") from exc
    result: List[Tuple[pathlib.Path, os.stat_result]] = []
    for child in children:
        child_metadata = _lstat_safe(child, label=f"{label} entry")
        assert child_metadata is not None
        result.append((child, child_metadata))
    return result


def commands_root_for(
    bridge_dir: pathlib.Path, *, expected_bridge_identity: Optional[Tuple[int, int]] = None,
) -> pathlib.Path:
    """Return the non-following absolute command root for a Bridge directory."""

    bridge = _absolute(pathlib.Path(bridge_dir))
    bridge_metadata = _lstat_safe(bridge, label="Bridge directory")
    assert bridge_metadata is not None
    if not stat.S_ISDIR(bridge_metadata.st_mode):
        raise CommandCustodyConflict(f"Bridge directory is not a directory: {bridge}")
    if expected_bridge_identity is not None and _identity(bridge_metadata) != expected_bridge_identity:
        raise CommandCustodyConflict("Bridge directory differs from the caller's pinned identity")
    _bind_directory_identity(bridge, bridge_metadata, label="Bridge directory")
    return _ensure_directory(bridge / "commands", label="commands directory")


def lifecycle_lock_path(bridge_dir: pathlib.Path) -> pathlib.Path:
    return commands_root_for(bridge_dir) / LIFECYCLE_LOCK_NAME


def lifecycle_recovery_dir(bridge_dir: pathlib.Path) -> pathlib.Path:
    return _ensure_directory(
        commands_root_for(bridge_dir) / LIFECYCLE_RECOVERY_DIR_NAME,
        label="lifecycle recovery directory",
    )


def _ensure_layout(bridge_dir: pathlib.Path) -> Dict[str, pathlib.Path]:
    bridge = _absolute(pathlib.Path(bridge_dir))
    root = commands_root_for(bridge)
    result: Dict[str, pathlib.Path] = {}
    for stage in COMMAND_STAGES:
        result[stage] = _ensure_directory(root / stage, label=f"commands/{stage} directory")
    result[LIFECYCLE_RECOVERY_DIR_NAME] = _ensure_directory(
        root / LIFECYCLE_RECOVERY_DIR_NAME,
        label="lifecycle recovery directory",
    )
    allowed = set(COMMAND_STAGES) | {LIFECYCLE_RECOVERY_DIR_NAME, LIFECYCLE_LOCK_NAME}
    for child, _metadata in _directory_entries(root, label="commands directory"):
        if child.name not in allowed:
            raise CommandCustodyConflict(f"unexpected commands-root entry: {child}")
    return result


def _lock_state(key: str) -> _ThreadOwner:
    with _OWNER_REGISTRY_GUARD:
        state = _OWNER_REGISTRY.get(key)
        if state is None:
            state = _ThreadOwner(condition=threading.Condition())
            _OWNER_REGISTRY[key] = state
        return state


def _reserve_thread_owner(
    state: _ThreadOwner,
    *,
    deadline: float,
    thread_id: int,
) -> bool:
    with state.condition:
        if state.owner_thread_id == thread_id:
            if state.depth >= _MAX_THREAD_RECURSION:
                raise CommandCustodyConflict("lifecycle lock recursion depth exceeded")
            state.depth += 1
            return True
        while state.owner_thread_id is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandCustodyTimeout("timed out waiting for the in-process lifecycle owner")
            state.condition.wait(timeout=min(_POLL_SECONDS, remaining))
        state.owner_thread_id = thread_id
        state.depth = 1
        return False


def _release_thread_owner(state: _ThreadOwner, *, thread_id: int) -> None:
    with state.condition:
        if state.owner_thread_id != thread_id:
            raise CommandCustodyError("lifecycle lock released by a non-owner thread")
        state.depth -= 1
        if state.depth > 0:
            return
        state.owner_thread_id = None
        state.descriptor = None
        state.identity = None
        state.parent_identity = None
        state.condition.notify_all()


def _prepare_lock_header(descriptor: int, lock_path: pathlib.Path) -> None:
    current = os.fstat(descriptor)
    if current.st_size == 0:
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(_LOCK_HEADER)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while initializing lifecycle lock")
            view = view[written:]
        os.fsync(descriptor)
        return
    os.lseek(descriptor, 0, os.SEEK_SET)
    value = os.read(descriptor, len(_LOCK_HEADER) + 1)
    if value != _LOCK_HEADER:
        raise CommandCustodyConflict(f"lifecycle lock header is not recognized: {lock_path}")


def _open_lock_file(lock_path: pathlib.Path) -> Tuple[int, Tuple[int, int]]:
    parent = lock_path.parent
    parent_metadata_before = _lstat_safe(parent, label="commands directory")
    assert parent_metadata_before is not None
    existing = _lstat_safe(lock_path, label="lifecycle lock", allow_missing=True)
    if existing is not None:
        if not stat.S_ISREG(existing.st_mode):
            raise CommandCustodyConflict(f"lifecycle lock is not a regular file: {lock_path}")
        if int(existing.st_nlink) != 1:
            raise CommandCustodyConflict(f"lifecycle lock is hard-linked: {lock_path}")

    flags = os.O_CREAT | os.O_RDWR
    binary_flag = getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(lock_path), flags | binary_flag, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
            raise CommandCustodyConflict(
                f"opened lifecycle lock is not a private regular file: {lock_path}"
            )
        named = _lstat_safe(lock_path, label="lifecycle lock")
        assert named is not None
        if _identity(opened) != _identity(named):
            raise CommandCustodyConflict("lifecycle lock name changed during open")
        parent_metadata_after = _lstat_safe(parent, label="commands directory")
        assert parent_metadata_after is not None
        if _identity(parent_metadata_before) != _identity(parent_metadata_after):
            raise CommandCustodyConflict("commands directory identity changed during lifecycle-lock open")
        # Create a byte to lock before trying msvcrt.locking on Windows.  The
        # header is constant and is written before ownership is acquired; the
        # actual mutation boundary remains the OS lock below.
        _prepare_lock_header(descriptor, lock_path)
        return descriptor, _identity(opened)
    except Exception:
        os.close(descriptor)
        raise


def _try_lock_descriptor(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _ensure_lock_header(descriptor: int, lock_path: pathlib.Path) -> None:
    current = os.fstat(descriptor)
    if current.st_size != len(_LOCK_HEADER):
        raise CommandCustodyConflict(f"lifecycle lock header length is not recognized: {lock_path}")
    os.lseek(descriptor, 0, os.SEEK_SET)
    value = os.read(descriptor, len(_LOCK_HEADER) + 1)
    if value != _LOCK_HEADER:
        raise CommandCustodyConflict(f"lifecycle lock header is not recognized: {lock_path}")


@contextlib.contextmanager
def lifecycle_custody(
    bridge_dir: pathlib.Path,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    expected_bridge_identity: Optional[Tuple[int, int]] = None,
) -> Iterator[None]:
    """Acquire the shared lifecycle lock with explicit thread recursion."""

    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise CommandCustodyError("lifecycle lock timeout must be numeric")
    if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0 or timeout_seconds > 300:
        raise CommandCustodyError("lifecycle lock timeout is outside the bounded range")

    bridge = _absolute(pathlib.Path(bridge_dir))
    commands_root_for(bridge, expected_bridge_identity=expected_bridge_identity)
    lock_path = lifecycle_lock_path(bridge)
    key = os.path.normcase(os.path.abspath(str(lock_path)))
    state = _lock_state(key)
    thread_id = threading.get_ident()
    deadline = time.monotonic() + float(timeout_seconds)
    nested = _reserve_thread_owner(state, deadline=deadline, thread_id=thread_id)
    if nested:
        try:
            named = _lstat_safe(lock_path, label="lifecycle lock")
            parent = _lstat_safe(lock_path.parent, label="commands directory")
            assert named is not None and parent is not None
            if state.identity is None or _identity(named) != state.identity:
                raise CommandCustodyConflict("lifecycle lock name changed during recursive acquisition")
            if state.parent_identity is None or _identity(parent) != state.parent_identity:
                raise CommandCustodyConflict("commands directory changed during recursive acquisition")
            yield
        finally:
            _release_thread_owner(state, thread_id=thread_id)
        return

    descriptor: Optional[int] = None
    locked = False
    try:
        while descriptor is None:
            try:
                descriptor, identity = _open_lock_file(lock_path)
            except OSError as exc:
                if not isinstance(exc, PermissionError) and getattr(exc, "errno", None) not in {
                    errno.EACCES,
                    errno.EBUSY,
                    errno.EPERM,
                }:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandCustodyTimeout(
                        "timed out waiting for the OS lifecycle lock to become openable"
                    ) from exc
                time.sleep(min(_POLL_SECONDS, remaining))
        while not _try_lock_descriptor(descriptor):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandCustodyTimeout("timed out waiting for the OS lifecycle lock")
            time.sleep(min(_POLL_SECONDS, remaining))
        locked = True
        _ensure_lock_header(descriptor, lock_path)
        named = _lstat_safe(lock_path, label="lifecycle lock")
        assert named is not None
        if _identity(named) != identity:
            raise CommandCustodyConflict("lifecycle lock name changed after OS lock acquisition")
        state.descriptor = descriptor
        state.identity = identity
        parent = _lstat_safe(lock_path.parent, label="commands directory")
        assert parent is not None
        state.parent_identity = _identity(parent)
        yield
    finally:
        try:
            if locked and descriptor is not None:
                try:
                    _unlock_descriptor(descriptor)
                except OSError:
                    pass
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            _release_thread_owner(state, thread_id=thread_id)


def _validate_command_id(value: Any, *, label: str = "command id") -> str:
    if not isinstance(value, str) or not _COMMAND_ID_RE.fullmatch(value):
        raise CommandCustodyConflict(f"{label} is not canonical: {value!r}")
    if value in {".", ".."} or value.startswith(".") or ".." in value:
        raise CommandCustodyConflict(f"{label} is not canonical: {value!r}")
    return value


def _validate_queue_name(name: str) -> str:
    return _validate_command_id(name, label="queue filename")


def _queue_stage_dir(bridge_dir: pathlib.Path, stage: str) -> pathlib.Path:
    if stage not in COMMAND_STAGES:
        raise CommandCustodyError(f"unknown command stage: {stage}")
    return _ensure_directory(
        commands_root_for(bridge_dir) / stage,
        label=f"commands/{stage} directory",
    )


def queue_stage_path(bridge_dir: pathlib.Path, stage: str, name: str) -> pathlib.Path:
    safe_name = _validate_queue_name(name)
    return _queue_stage_dir(bridge_dir, stage) / safe_name


def _validate_regular_queue_file(path: pathlib.Path, *, label: str) -> os.stat_result:
    metadata = _lstat_safe(path, label=label)
    assert metadata is not None
    if not stat.S_ISREG(metadata.st_mode):
        raise CommandCustodyConflict(f"{label} is not a regular file: {path}")
    if int(metadata.st_nlink) != 1:
        raise CommandCustodyConflict(f"{label} is hard-linked: {path}")
    return metadata


def _validate_directory(path: pathlib.Path, *, label: str) -> os.stat_result:
    metadata = _lstat_safe(path, label=label)
    assert metadata is not None
    if not stat.S_ISDIR(metadata.st_mode):
        raise CommandCustodyConflict(f"{label} is not a directory: {path}")
    return metadata


def _open_share_delete_descriptor(path: pathlib.Path) -> int:
    """Open for reading without denying a concurrent rename or delete.

    ``os.open`` on Windows omits FILE_SHARE_DELETE, so while Bridge read a
    command file the executor's File.Move/File.Replace of it failed with a
    sharing violation; after dispatch that became outcome_unknown and paused
    the game (code review 2026-09-26, L10).
    """

    create_file, close_handle = _windows_file_api()
    generic_read = 0x80000000
    share_all = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
    open_existing = 3
    # FILE_FLAG_OPEN_REPARSE_POINT: never follow a link planted after lstat.
    flags = 0x80 | 0x00200000
    handle = create_file(str(path), generic_read, share_all, None, open_existing, flags, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        import msvcrt

        return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        close_handle(handle)
        raise


_WINDOWS_FILE_API: Optional[Tuple[Any, Any]] = None


def _windows_file_api() -> Tuple[Any, Any]:
    global _WINDOWS_FILE_API
    if _WINDOWS_FILE_API is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        _WINDOWS_FILE_API = (create_file, close_handle)
    return _WINDOWS_FILE_API


def _open_read_descriptor(path: pathlib.Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        if os.name == "nt":
            return _open_share_delete_descriptor(path)
        return os.open(str(path), flags | no_follow)
    except OSError as exc:
        raise CommandCustodyError(f"cannot open lifecycle file: {path}") from exc


def read_bytes_share_delete(path: pathlib.Path) -> bytes:
    """Read a whole file without blocking a concurrent executor rename."""

    try:
        descriptor = _open_read_descriptor(path)
    except CommandCustodyError as exc:
        cause = exc.__cause__
        raise cause if isinstance(cause, OSError) else OSError(str(exc)) from exc
    try:
        chunks: List[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def read_stable_bytes(
    path: pathlib.Path,
    *,
    label: str = "queue file",
    max_bytes: Optional[int] = None,
) -> Tuple[bytes, Tuple[int, int], int]:
    """Read bytes while proving path and open-handle identity stayed stable."""

    before = _validate_regular_queue_file(path, label=label)
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1 or before.st_size > max_bytes):
        raise CommandCustodyError(f"{label} exceeds its bounded read limit")
    descriptor = _open_read_descriptor(path)
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise CommandCustodyConflict(f"{label} identity changed before read: {path}")
        chunks: List[bytes] = []
        total_bytes = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise CommandCustodyError(f"{label} grew beyond its bounded read limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = _lstat_safe(path, label=label, allow_missing=True)
        if named is None:
            raise CommandCustodyConflict(f"{label} was moved away during read: {path}")
        if _identity(after) != _identity(before) or _identity(named) != _identity(before):
            raise CommandCustodyConflict(f"{label} identity changed during read: {path}")
        if _mtime_ns(after) != _mtime_ns(before) or int(after.st_size) != int(before.st_size):
            raise CommandCustodyConflict(f"{label} changed during read: {path}")
        value = b"".join(chunks)
        if len(value) != int(before.st_size):
            raise CommandCustodyConflict(f"{label} size changed during read: {path}")
        return value, _identity(before), _mtime_ns(before)
    finally:
        os.close(descriptor)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _strict_json_loads(value: bytes, *, label: str) -> Any:
    def reject(token: str) -> None:
        raise ValueError(f"non-finite JSON constant: {token}")

    try:
        return json.loads(value.decode("utf-8-sig"), parse_constant=reject)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CommandCustodyError(f"{label} is not valid strict JSON") from exc


def _json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise CommandCustodyError("value cannot be encoded as strict JSON") from exc
    return (encoded + "\n").encode("utf-8")


def _unique_recovery_stem(command_id: str, suffix: str) -> str:
    safe = _validate_queue_name(command_id)
    return f"{safe}.{os.getpid()}.{time.time_ns()}.{uuid.uuid4().hex}.{suffix}"


def _write_temp_bytes(directory: pathlib.Path, payload: bytes, *, prefix: str) -> pathlib.Path:
    _validate_directory(directory, label="lifecycle temporary directory")
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(directory))
    path = pathlib.Path(raw_path)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short lifecycle temporary write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _validate_regular_queue_file(path, label="lifecycle temporary")
    return path


def _unlink_private(path: pathlib.Path) -> None:
    try:
        metadata = _lstat_safe(path, label="lifecycle temporary", allow_missing=True)
        if metadata is None:
            return
        if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
            return
        path.unlink()
    except OSError:
        pass


def _fsync_directory(path: pathlib.Path) -> bool:
    """Fsync a directory where the platform exposes that operation."""

    _validate_directory(path, label="lifecycle directory")
    if os.name == "nt":
        # MoveFileExW is called with WRITE_THROUGH below.  Windows does not
        # generally expose a directory descriptor suitable for os.fsync.
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EPERM, errno.EACCES}:
            return False
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _rename_noreplace_windows(source: pathlib.Path, target: pathlib.Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file_ex.restype = ctypes.c_int
    if move_file_ex(str(source), str(target), _MOVEFILE_WRITE_THROUGH):
        return
    error = ctypes.get_last_error() or errno.EIO
    if error in {2, 3}:
        raise FileNotFoundError(error, os.strerror(error), str(source))
    if error in {80, 183}:
        raise FileExistsError(error, os.strerror(error), str(target))
    raise OSError(error, os.strerror(error), str(source), str(target))


def _rename_noreplace_posix(source: pathlib.Path, target: pathlib.Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise CommandCustodyError("atomic no-replace rename is unavailable on this platform")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(str(source)),
        _AT_FDCWD,
        os.fsencode(str(target)),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno() or errno.EIO
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(target))
    if error == errno.ENOENT:
        raise FileNotFoundError(error, os.strerror(error), str(source))
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise CommandCustodyError("atomic no-replace rename is unavailable on this platform")
    raise OSError(error, os.strerror(error), str(source), str(target))


def _atomic_rename_noreplace(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    expected_source_identity: Optional[Tuple[int, int]] = None,
) -> Tuple[int, int]:
    """Atomically rename without overwriting target and verify identity."""

    source = _absolute(source)
    target = _absolute(target)
    source_metadata = _validate_regular_queue_file(source, label="rename source")
    source_parent_metadata = _validate_directory(source.parent, label="rename source directory")
    target_parent_metadata = _validate_directory(target.parent, label="rename target directory")
    if expected_source_identity is not None and _identity(source_metadata) != expected_source_identity:
        raise CommandCustodyConflict("rename source identity does not match its claimed envelope")
    _lstat_safe(target.parent, label="rename target directory")
    if _lstat_safe(target, label="rename target", allow_missing=True) is not None:
        raise CommandCustodyConflict(f"rename target already exists: {target}")
    if os.name == "nt":
        _rename_noreplace_windows(source, target)
    else:
        _rename_noreplace_posix(source, target)
    target_metadata = _validate_regular_queue_file(target, label="renamed target")
    if _identity(target_metadata) != _identity(source_metadata):
        raise CommandCustodyUnknown("rename completed with an unexpected target identity")
    source_parent_after = _validate_directory(source.parent, label="rename source directory")
    target_parent_after = _validate_directory(target.parent, label="rename target directory")
    if _identity(source_parent_after) != _identity(source_parent_metadata):
        raise CommandCustodyUnknown("rename source directory changed at the boundary")
    if _identity(target_parent_after) != _identity(target_parent_metadata):
        raise CommandCustodyUnknown("rename target directory changed at the boundary")
    _fsync_directory(source.parent)
    if not _same_path(source.parent, target.parent):
        _fsync_directory(target.parent)
    return _identity(target_metadata)


def _rewrite_file_exact(
    path: pathlib.Path,
    payload: bytes,
    *,
    expected_identity: Tuple[int, int],
    label: str,
) -> Tuple[int, int]:
    """Rewrite only a journal/evidence file while retaining its inode."""

    current = _validate_regular_queue_file(path, label=label)
    if _identity(current) != expected_identity:
        raise CommandCustodyConflict(f"{label} identity changed before rewrite")
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise CommandCustodyError(f"{label} cannot be opened for rewrite") from exc
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != expected_identity:
            raise CommandCustodyConflict(f"{label} identity changed during rewrite")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while rewriting {label}")
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if int(after.st_size) != len(payload):
            raise CommandCustodyUnknown(f"{label} size changed after rewrite")
    except OSError as exc:
        raise CommandCustodyUnknown(f"{label} rewrite crossed an uncertain boundary") from exc
    finally:
        os.close(descriptor)
    named = _lstat_safe(path, label=label)
    assert named is not None
    if _identity(named) != expected_identity:
        raise CommandCustodyUnknown(f"{label} path identity changed after rewrite")
    return expected_identity


def _validate_command_envelope(
    value: Any,
    *,
    expected_id: Optional[str] = None,
    expected_stage: Optional[str] = None,
    expected_status: Optional[str] = None,
    label: str,
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CommandCustodyConflict(f"{label} is not a JSON object")
    if value.get("schema") not in (COMMAND_SCHEMA, AUTONOMOUS_COMMAND_SCHEMA):
        raise CommandCustodyConflict(f"{label} schema is not canonical")
    command_id = _validate_command_id(value.get("id"), label=f"{label} id")
    if expected_id is not None and command_id != expected_id:
        raise CommandCustodyConflict(f"{label} id does not match its claimed role")
    if value.get("schema") == AUTONOMOUS_COMMAND_SCHEMA:
        from agent.action_policy import autonomous_identifier_is_valid

        if any(not autonomous_identifier_is_valid(value.get(key)) for key in ("run_id", "controller_id")):
            raise CommandCustodyConflict(f"{label} autonomous authority identity is invalid")
        if value.get("mode") not in ("model_selected", "suggested") or value.get("requires_approval") is not True:
            raise CommandCustodyConflict(f"{label} autonomous authorization contract is invalid")
    pending_status = (
        AUTONOMOUS_PENDING_STATUS
        if value.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
        else PENDING_STATUS
    )
    if expected_stage == "pending" and value.get("status") != pending_status:
        raise CommandCustodyConflict(f"{label} status is not pending approval")
    if expected_stage == "approved" and value.get("status") != APPROVED_STATUS:
        raise CommandCustodyConflict(f"{label} status is not approved")
    if expected_status is not None and value.get("status") != expected_status:
        raise CommandCustodyConflict(f"{label} status is not canonical")
    return value


def _done_status_for(command: Mapping[str, Any]) -> str:
    """Return the canonical ``done/`` status for one command.

    ``noop`` and ``focus_note`` never mutate game state; the executor completes
    them directly in ``done/status=verified`` (docs/protocol.md, command
    lifecycle).  Every mutating command must reach ``done`` only through
    snapshot attestation as ``confirmed``, so a mutating ``verified`` record
    still fails closed.
    """

    if command.get("action") in NON_MUTATING_ACTIONS and command.get("status") == "verified":
        return "verified"
    return "confirmed"


def _command_id_from_bytes(value: bytes, *, label: str) -> str:
    parsed = _strict_json_loads(value, label=label)
    if not isinstance(parsed, dict):
        raise CommandCustodyConflict(f"queue copy is not a JSON object: {label}")
    return _validate_command_id(parsed.get("id"), label=f"{label} command id")


class _TransientQueueState(Exception):
    """An inventory state the unlocked executor passes through in milliseconds."""

    def __init__(self, error: CommandCustodyError) -> None:
        super().__init__(str(error))
        self.error = error


def _scan_command_locations(bridge_dir: pathlib.Path) -> List[_CommandLocation]:
    """Return the strict inventory once it is settled.

    The C# executor claims and finishes commands without the lifecycle lock,
    so a scan can meet a temp/backup sibling, ``status=executing`` still in
    ``approved/``, a file moved away or replaced between listing and read, or
    one id in two stages during a copy-then-delete move.  Each settles within
    milliseconds; rescan with a short backoff and fail closed with the same
    error only if it persists (code review 2026-09-26, L10).
    """

    for attempt in range(_SCAN_SETTLE_ATTEMPTS):
        last = attempt + 1 >= _SCAN_SETTLE_ATTEMPTS
        try:
            locations = _scan_command_locations_once(bridge_dir)
        except _TransientQueueState as transient:
            if last:
                raise transient.error from transient.error.__cause__
        else:
            ids = [str(item.command["id"]) for item in locations]
            if last or len(ids) == len(set(ids)):
                return locations
        time.sleep(_SCAN_SETTLE_DELAY_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def _scan_command_locations_once(bridge_dir: pathlib.Path) -> List[_CommandLocation]:
    layout = _ensure_layout(bridge_dir)
    locations: List[_CommandLocation] = []
    for stage in COMMAND_STAGES:
        directory = layout[stage]
        try:
            entries = _directory_entries(directory, label=f"commands/{stage} directory")
        except CommandCustodyError as exc:
            # Typically an entry moved away between listing and lstat.
            raise _TransientQueueState(exc)
        for path, listed in entries:
            if path.suffix.lower() != ".json":
                error = CommandCustodyUnknown(f"unexpected protected-stage material: {path}")
                if _EXECUTOR_TRANSIENT_NAME_RE.fullmatch(path.name):
                    raise _TransientQueueState(error)
                raise error
            try:
                raw, identity, mtime_ns = read_stable_bytes(
                    path,
                    label=f"commands/{stage}/{path.name}",
                )
                parsed = _strict_json_loads(raw, label=f"commands/{stage}/{path.name}")
                if not isinstance(parsed, dict):
                    raise CommandCustodyConflict("protected command is not a JSON object")
                command_id = _validate_command_id(
                    parsed.get("id"),
                    label=f"commands/{stage}/{path.name} id",
                )
            except CommandCustodyError as exc:
                error = exc
                if not isinstance(exc, CommandCustodyUnknown):
                    error = CommandCustodyUnknown(f"protected command could not be validated: {path}")
                    error.__cause__ = exc
                current = _lstat_safe(path, label=f"commands/{stage}/{path.name}", allow_missing=True)
                if current is None or _identity(current) != _identity(listed):
                    # Moved away or replaced by an atomic executor write.
                    raise _TransientQueueState(error)
                raise error
            if path.name != f"{command_id}.json":
                raise CommandCustodyConflict(
                    f"protected command filename is not canonical: {path}"
                )
            expected_statuses = {
                "pending": {
                    AUTONOMOUS_PENDING_STATUS
                    if parsed.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
                    else PENDING_STATUS
                },
                "approved": {"approved"},
                "done": {_done_status_for(parsed)},
                "failed": {"failed"},
                "rejected": {"rejected"},
                "quarantine": {"quarantined"},
            }.get(stage)
            if expected_statuses is not None and parsed.get("status") not in expected_statuses:
                error = CommandCustodyConflict(
                    f"protected command status is not canonical for {stage}: {path}"
                )
                if (
                    stage == "approved"
                    and parsed.get("status") == "executing"
                    and isinstance(parsed.get("execution_claim"), dict)
                ):
                    # Claimed in place; the executor renames it to executing/.
                    raise _TransientQueueState(error)
                raise error
            locations.append(
                _CommandLocation(
                    stage=stage,
                    path=path,
                    payload=raw,
                    identity=identity,
                    mtime_ns=mtime_ns,
                    command=parsed,
                )
            )
    return locations


def _locations_for_id(
    bridge_dir: pathlib.Path,
    command_id: str,
) -> List[Tuple[str, pathlib.Path, Optional[Dict[str, Any]]]]:
    wanted = _validate_command_id(command_id)
    return [
        (item.stage, item.path, item.command)
        for item in _scan_command_locations(bridge_dir)
        if item.command.get("id") == wanted
    ]


def _assert_no_duplicate_locations(locations: Sequence[_CommandLocation]) -> None:
    grouped: Dict[str, List[_CommandLocation]] = {}
    for item in locations:
        grouped.setdefault(str(item.command["id"]), []).append(item)
    duplicates = [items for items in grouped.values() if len(items) > 1]
    if duplicates:
        rendered = ";".join(
            ",".join(f"{item.stage}/{item.path.name}" for item in items)
            for items in duplicates
        )
        raise CommandCustodyConflict(f"duplicate protected command id: {rendered}")


def _validate_pending_observation(
    observation: PendingEnvelopeObservation,
    *,
    expected_path: pathlib.Path,
) -> None:
    """Validate an observation's shape and canonical path before rechecking it."""

    if not isinstance(observation, PendingEnvelopeObservation):
        raise CommandCustodyConflict("pending envelope observation has an invalid type")
    if not isinstance(observation.path, pathlib.Path):
        raise CommandCustodyConflict("pending envelope observation path is invalid")
    if not isinstance(observation.payload, bytes):
        raise CommandCustodyConflict("pending envelope observation payload is not immutable bytes")
    if (
        not isinstance(observation.identity, tuple)
        or len(observation.identity) != 2
        or any(not isinstance(part, int) or isinstance(part, bool) for part in observation.identity)
    ):
        raise CommandCustodyConflict("pending envelope observation identity is invalid")
    if not isinstance(observation.mtime_ns, int) or isinstance(observation.mtime_ns, bool):
        raise CommandCustodyConflict("pending envelope observation mtime is invalid")
    if not _same_path(observation.path, expected_path):
        raise CommandCustodyConflict("pending envelope observation path is not canonical")


def _observe_pending_envelope_locked(
    bridge_dir: pathlib.Path,
    source: pathlib.Path,
    *,
    expected_source: Mapping[str, Any],
) -> PendingEnvelopeObservation:
    """Return one stable pending snapshot while the lifecycle lock is held."""

    if not isinstance(expected_source, Mapping):
        raise CommandCustodyError("expected pending source must be a mapping")
    source = _absolute(source)
    expected_source_path = queue_stage_path(bridge_dir, "pending", source.name)
    if not _same_path(source, expected_source_path):
        raise CommandCustodyConflict("pending envelope observation path is outside the canonical queue")
    command_id = _validate_command_id(source.stem, label="pending observation command id")

    # Observe must never resolve recovery evidence.  The lock only makes this
    # blocker check and the following inventory scan one stable boundary.
    locations = _scan_command_locations(bridge_dir)
    _assert_no_duplicate_locations(locations)
    observed_statuses = {
        "approved": "approved",
        "failed": "failed",
        "rejected": "rejected",
        "quarantine": "quarantined",
    }
    for item in locations:
        if item.stage == "pending":
            _validate_command_envelope(
                item.command,
                expected_id=str(item.command["id"]),
                expected_stage="pending",
                label=f"commands/{item.stage}/{item.path.name}",
            )
        else:
            _validate_command_envelope(
                item.command,
                expected_id=str(item.command["id"]),
                expected_status=(
                    _done_status_for(item.command)
                    if item.stage == "done"
                    else observed_statuses.get(item.stage)
                ),
                label=f"commands/{item.stage}/{item.path.name}",
            )
    owned_locations = [item for item in locations if item.command.get("id") == command_id]
    if len(owned_locations) != 1:
        raise CommandCustodyConflict(
            f"pending envelope observation command id is not uniquely owned: {command_id}"
        )
    location = owned_locations[0]
    if location.stage != "pending" or not _same_path(location.path, source):
        raise CommandCustodyConflict(
            f"pending envelope observation source is not the sole pending owner: {command_id}"
        )
    actual_command = _validate_command_envelope(
        location.command,
        expected_id=command_id,
        expected_stage="pending",
        label="pending observation source",
    )
    expected_bytes = _json_bytes(dict(expected_source))
    expected_command = _validate_command_envelope(
        _strict_json_loads(expected_bytes, label="expected pending source"),
        expected_id=command_id,
        expected_stage="pending",
        label="expected pending source",
    )
    del expected_command
    if expected_bytes != _json_bytes(actual_command):
        raise CommandCustodyConflict("pending source envelope does not match its observed semantics")
    return PendingEnvelopeObservation(
        path=location.path,
        payload=location.payload,
        identity=location.identity,
        mtime_ns=location.mtime_ns,
    )


def observe_pending_envelope(
    bridge_dir: pathlib.Path,
    source: pathlib.Path,
    *,
    expected_source: Mapping[str, Any],
) -> PendingEnvelopeObservation:
    """Observe one pending envelope without migrating source, target, or journal."""

    with lifecycle_custody(bridge_dir):
        if lifecycle_recovery_blockers(bridge_dir):
            raise CommandCustodyConflict(
                "pending envelope observation requires explicit lifecycle recovery"
            )
        return _observe_pending_envelope_locked(
            bridge_dir,
            source,
            expected_source=expected_source,
        )


def _recovery_payload_paths(
    recovery_dir: pathlib.Path,
    command_id: str,
    transaction: str,
) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    safe_id = _validate_command_id(command_id)
    if not _TRANSACTION_RE.fullmatch(transaction):
        raise CommandCustodyConflict("lifecycle transaction id is not bounded")
    prefix = f"{safe_id}.{transaction}"
    return (
        recovery_dir / f"{prefix}.original.bytes",
        recovery_dir / f"{prefix}.final.bytes",
        recovery_dir / f"{prefix}.lifecycle.json",
    )


def _relative_path(bridge_dir: pathlib.Path, path: pathlib.Path) -> str:
    bridge = _absolute(bridge_dir)
    value = str(_absolute(path).relative_to(bridge)).replace("\\", "/")
    if len(value) > _MAX_JOURNAL_STRING:
        raise CommandCustodyConflict("lifecycle journal path is too long")
    return value


def _journal_value(
    *,
    bridge_dir: pathlib.Path,
    source: pathlib.Path,
    target: pathlib.Path,
    command_id: str,
    operation: str,
    source_status: str,
    original_hash: str,
    final_hash: str,
    original_identity: Tuple[int, int],
    original_mtime_ns: int,
    original_payload: pathlib.Path,
    original_payload_identity: Tuple[int, int],
    final_payload: pathlib.Path,
    final_payload_identity: Tuple[int, int],
    transaction: str,
    phase: str,
    journal_identity: Tuple[int, int],
    source_stage: str = "pending",
) -> Dict[str, Any]:
    return {
        "schema": LIFECYCLE_JOURNAL_SCHEMA,
        "transaction": transaction,
        "command_id": command_id,
        "operation": operation,
        "source_stage": source_stage,
        "source_status": source_status,
        "target_stage": operation,
        "target_status": DISPOSITION_STATUSES[operation],
        "source": _relative_path(bridge_dir, source),
        "target": _relative_path(bridge_dir, target),
        "original_sha256": original_hash,
        "final_sha256": final_hash,
        "original_identity": list(original_identity),
        "original_mtime_ns": int(original_mtime_ns),
        "original_payload": _relative_path(bridge_dir, original_payload),
        "original_payload_identity": list(original_payload_identity),
        "final_payload": _relative_path(bridge_dir, final_payload),
        "final_payload_identity": list(final_payload_identity),
        "phase": phase,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "journal_identity": list(journal_identity),
        "journal_sha256": "",
    }


def _journal_digest(journal: Mapping[str, Any]) -> str:
    value = dict(journal)
    value["journal_sha256"] = ""
    return sha256_bytes(_json_bytes(value))


def _journal_bytes(journal: Mapping[str, Any]) -> bytes:
    value = dict(journal)
    value["journal_sha256"] = _journal_digest(value)
    return _json_bytes(value)


def _atomic_create_bytes(path: pathlib.Path, payload: bytes) -> Tuple[int, int]:
    """Create a complete file without overwriting an existing name."""

    path = _absolute(path)
    parent_metadata = _validate_directory(path.parent, label="create target directory")
    if _lstat_safe(path, label="create target", allow_missing=True) is not None:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(path))
    temporary = _write_temp_bytes(path.parent, payload, prefix=".lifecycle-create-")
    try:
        identity = _atomic_rename_noreplace(temporary, path)
        temporary = None
        parent_after = _validate_directory(path.parent, label="create target directory")
        if _identity(parent_after) != _identity(parent_metadata):
            raise CommandCustodyUnknown("create target directory changed at the boundary")
        return identity
    finally:
        if temporary is not None:
            _unlink_private(temporary)


def _publish_pending_command_locked(
    bridge_dir: pathlib.Path,
    command: Mapping[str, Any],
) -> pathlib.Path:
    """Publish one complete pending envelope under shared custody."""

    if not isinstance(command, Mapping):
        raise CommandCustodyError("pending command must be a mapping")
    payload = _json_bytes(dict(command))
    parsed = _validate_command_envelope(
        _strict_json_loads(payload, label="pending command"),
        expected_stage="pending",
        label="pending command",
    )
    command_id = str(parsed["id"])
    locations = _scan_command_locations(bridge_dir)
    if any(item.command.get("id") == command_id for item in locations):
        raise CommandCustodyConflict(f"pending publication would duplicate command id: {command_id}")
    path = queue_stage_path(bridge_dir, "pending", f"{command_id}.json")
    try:
        identity = _atomic_create_bytes(path, payload)
    except FileExistsError as exc:
        raise CommandCustodyConflict(f"pending publication target already exists: {path}") from exc
    try:
        observed, observed_identity, _mtime = read_stable_bytes(path, label="new pending command")
    except CommandCustodyError as exc:
        raise CommandCustodyUnknown("new pending command could not be revalidated") from exc
    if observed_identity != identity or observed != payload:
        raise CommandCustodyUnknown("new pending command changed after create-only publication")
    locations_after = _scan_command_locations(bridge_dir)
    owned = [item for item in locations_after if item.command.get("id") == command_id]
    if len(owned) != 1 or not _same_path(owned[0].path, path) or owned[0].identity != identity:
        raise CommandCustodyUnknown("pending publication produced an unexpected command inventory")
    return path


def publish_pending_command(
    bridge_dir: pathlib.Path,
    command: Mapping[str, Any],
) -> pathlib.Path:
    """Publish one complete pending envelope under shared custody."""

    with lifecycle_custody(bridge_dir):
        _strict_locked_preflight(bridge_dir)
        return _publish_pending_command_locked(bridge_dir, command)


def _transition_pending_envelope_locked(
    bridge_dir: pathlib.Path,
    source: pathlib.Path,
    target: pathlib.Path,
    final_payload: Mapping[str, Any],
    *,
    expected_source: Optional[Mapping[str, Any]],
    expected_observation: Optional[PendingEnvelopeObservation] = None,
    operation: str,
    source_stage: str = "pending",
) -> Optional[pathlib.Path]:
    if operation not in DISPOSITION_STAGES:
        raise CommandCustodyError(f"unsupported pending disposition: {operation}")
    if source_stage not in COMMAND_STAGES:
        raise CommandCustodyError(f"unsupported disposition source stage: {source_stage}")
    source = _absolute(source)
    target = _absolute(target)
    expected_source_path = queue_stage_path(bridge_dir, source_stage, source.name)
    expected_target_path = queue_stage_path(bridge_dir, operation, source.name)
    if not _same_path(source, expected_source_path) or not _same_path(target, expected_target_path):
        raise CommandCustodyConflict("pending disposition paths are outside the canonical queue")
    if expected_observation is not None:
        if expected_source is None:
            raise CommandCustodyConflict("expected_observation requires expected_source")
        if not isinstance(expected_source, Mapping):
            raise CommandCustodyError("expected pending source must be a mapping")
        _validate_pending_observation(expected_observation, expected_path=expected_source_path)
    command_id = _validate_command_id(source.stem, label="pending disposition command id")
    final_bytes = _json_bytes(dict(final_payload))
    final_command = _validate_command_envelope(
        _strict_json_loads(final_bytes, label="pending disposition payload"),
        expected_id=command_id,
        expected_status=DISPOSITION_STATUSES[operation],
        label="pending disposition payload",
    )

    locations = _scan_command_locations(bridge_dir)
    owned_locations = [item for item in locations if item.command.get("id") == command_id]
    source_location = next((item for item in owned_locations if _same_path(item.path, source)), None)
    target_location = next((item for item in owned_locations if _same_path(item.path, target)), None)
    if any(not _same_path(item.path, source) and not _same_path(item.path, target) for item in owned_locations):
        raise CommandCustodyConflict(f"pending disposition command id has an unrelated location: {command_id}")
    if source_location is None:
        if expected_observation is not None:
            raise CommandCustodyConflict(
                f"pending disposition observed source is missing; stale observation rejected: {command_id}"
            )
        if target_location is not None and len(owned_locations) == 1 and target_location.payload == final_bytes:
            return target
        if not owned_locations:
            return None
        raise CommandCustodyConflict(f"pending disposition source is not the sole command owner: {command_id}")
    if target_location is not None:
        raise CommandCustodyConflict(f"pending disposition target already exists: {target}")
    if len(owned_locations) != 1 or source_location.stage != source_stage:
        raise CommandCustodyConflict(
            f"pending disposition command id is present in multiple queue locations: {command_id}"
        )

    original_bytes = source_location.payload
    original_identity = source_location.identity
    original_mtime_ns = source_location.mtime_ns
    original_command = _validate_command_envelope(
        _strict_json_loads(original_bytes, label="pending source"),
        expected_id=command_id,
        expected_stage=source_stage,
        label="pending source",
    )
    if final_command.get("schema") != original_command.get("schema"):
        raise CommandCustodyConflict("pending disposition must preserve the authorization version")
    if expected_observation is not None:
        if expected_observation.payload != original_bytes:
            raise CommandCustodyConflict("pending source bytes changed after observation")
        if expected_observation.identity != original_identity:
            raise CommandCustodyConflict("pending source identity changed after observation")
        if expected_observation.mtime_ns != original_mtime_ns:
            raise CommandCustodyConflict("pending source mtime changed after observation")
        expected_bytes = _json_bytes(dict(expected_source))
        _validate_command_envelope(
            _strict_json_loads(expected_bytes, label="expected pending source"),
            expected_id=command_id,
            expected_stage=source_stage,
            label="expected pending source",
        )
        if expected_bytes != _json_bytes(original_command):
            raise CommandCustodyConflict("pending source envelope does not match observed semantics")
    elif expected_source is not None and _json_bytes(dict(expected_source)) != original_bytes:
        raise CommandCustodyConflict("pending source envelope changed before disposition")

    recovery_dir = lifecycle_recovery_dir(bridge_dir)
    transaction = "tx-" + uuid.uuid4().hex
    original_path, final_path, journal_path = _recovery_payload_paths(
        recovery_dir,
        command_id,
        transaction,
    )
    try:
        original_payload_identity = _atomic_create_bytes(original_path, original_bytes)
        final_payload_identity = _atomic_create_bytes(final_path, final_bytes)
        journal = _journal_value(
            bridge_dir=bridge_dir,
            source=source,
            target=target,
            command_id=command_id,
            operation=operation,
            source_status=str(original_command["status"]),
            original_hash=sha256_bytes(original_bytes),
            final_hash=sha256_bytes(final_bytes),
            original_identity=original_identity,
            original_mtime_ns=original_mtime_ns,
            original_payload=original_path,
            original_payload_identity=original_payload_identity,
            final_payload=final_path,
            final_payload_identity=final_payload_identity,
            transaction=transaction,
            phase="staged",
            source_stage=source_stage,
            # The first body is intentionally not a recoverable record until
            # its real journal inode has been bound into the self-hash.
            journal_identity=(0, 1),
        )
        journal_identity = _atomic_create_bytes(journal_path, _journal_bytes(journal))
        journal["journal_identity"] = list(journal_identity)
        journal_raw = _journal_bytes(journal)
        _rewrite_file_exact(
            journal_path,
            journal_raw,
            expected_identity=journal_identity,
            label=f"lifecycle journal {journal_path.name}",
        )
        record = _JournalRecord(
            path=journal_path,
            raw=journal_raw,
            identity=journal_identity,
            journal=journal,
        )

        try:
            _atomic_rename_noreplace(
                final_path,
                target,
                expected_source_identity=final_payload_identity,
            )
        except Exception as exc:
            raise CommandCustodyUnknown(
                f"pending disposition terminal publication is uncertain for {command_id}"
            ) from exc
        observed, observed_identity, _target_mtime = read_stable_bytes(
            target,
            label="completed pending disposition",
        )
        if observed_identity != final_payload_identity or observed != final_bytes:
            raise CommandCustodyUnknown("pending disposition target failed final revalidation")
        record = _rewrite_journal_phase(record, "published")
        record = _rewrite_journal_phase(record, "completing")
        _unlink_owned(
            source,
            expected_identity=original_identity,
            expected_bytes=original_bytes,
            label="pending source",
        )
        _unlink_owned(
            original_path,
            expected_identity=original_payload_identity,
            expected_bytes=original_bytes,
            label="lifecycle original payload",
        )
        _unlink_owned(
            journal_path,
            expected_identity=journal_identity,
            expected_bytes=record.raw,
            label=f"lifecycle journal {journal_path.name}",
        )
        final_locations = _scan_command_locations(bridge_dir)
        final_owned = [item for item in final_locations if item.command.get("id") == command_id]
        if len(final_owned) != 1 or not _same_path(final_owned[0].path, target):
            raise CommandCustodyUnknown("pending disposition left an unexpected command inventory")
        return target
    except (CommandCustodyUnknown, CommandCustodyConflict):
        raise
    except Exception as exc:
        raise CommandCustodyUnknown(
            f"pending disposition outcome is unknown for {command_id}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
def transition_pending_envelope(
    bridge_dir: pathlib.Path,
    source: pathlib.Path,
    target: pathlib.Path,
    final_payload: Mapping[str, Any],
    *,
    expected_source: Optional[Mapping[str, Any]] = None,
    expected_observation: Optional[PendingEnvelopeObservation] = None,
    operation: str,
    source_stage: str = "pending",
) -> Optional[pathlib.Path]:
    """Move one exact pending envelope to rejected or quarantine custody.

    The source is never overwritten. A recovery journal and exact byte payloads
    remain if any post-write boundary is uncertain.

    ``source_stage`` names the queue stage the envelope is being taken from.  It
    defaults to ``pending``; ``approved`` is also allowed, because an approved
    command the executor never claimed can expire and must reach a terminal
    stage or its intent latches forever (2026-10-26 round 35).  Every other
    guarantee is unchanged: the source must be the canonical path for that
    stage, and the payload is validated exactly as before.
    """

    with lifecycle_custody(bridge_dir):
        _strict_locked_preflight(bridge_dir)
        return _transition_pending_envelope_locked(
            bridge_dir,
            source,
            target,
            final_payload,
            expected_source=expected_source,
            expected_observation=expected_observation,
            operation=operation,
            source_stage=source_stage,
        )


def _path_from_relative(bridge_dir: pathlib.Path, value: Any) -> pathlib.Path:
    if not isinstance(value, str) or not value or len(value) > _MAX_JOURNAL_STRING:
        raise CommandCustodyConflict("lifecycle journal path is invalid")
    if "\\" in value or "\x00" in value:
        raise CommandCustodyConflict("lifecycle journal path is not canonical")
    parts = value.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise CommandCustodyConflict("lifecycle journal path is not canonical")
    if parts[0] != "commands":
        raise CommandCustodyConflict("lifecycle journal path is outside commands")
    bridge = _absolute(bridge_dir)
    candidate = bridge.joinpath(*parts)
    expected = str(candidate.relative_to(bridge)).replace("\\", "/")
    if expected != value:
        raise CommandCustodyConflict("lifecycle journal path is not canonical")
    current = bridge
    for index, part in enumerate(parts):
        current = current / part
        is_final = index == len(parts) - 1
        metadata = _lstat_safe(
            current,
            label="lifecycle journal path component",
            allow_missing=is_final,
        )
        if metadata is None:
            if not is_final:
                raise CommandCustodyConflict("lifecycle journal path has a missing ancestor")
            continue
        if not is_final and not stat.S_ISDIR(metadata.st_mode):
            raise CommandCustodyConflict("lifecycle journal path ancestor is not a directory")
    return candidate


def _identity_from_value(value: Any, *, label: str) -> Tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            or item > 2**64 - 1
            for item in value
        )
        or (value[0] == 0 and value[1] == 0)
    ):
        raise CommandCustodyConflict(f"{label} is not a recorded file identity")
    return int(value[0]), int(value[1])


def _validate_journal_record(
    bridge_dir: pathlib.Path,
    path: pathlib.Path,
    raw: bytes,
    identity: Tuple[int, int],
) -> Dict[str, Any]:
    journal = _strict_json_loads(raw, label=f"lifecycle journal {path.name}")
    if not isinstance(journal, dict) or set(journal) != set(_JOURNAL_KEYS):
        raise CommandCustodyConflict("lifecycle journal schema/keyset is not exact")
    if journal.get("schema") != LIFECYCLE_JOURNAL_SCHEMA:
        raise CommandCustodyConflict("lifecycle journal schema is not recognized")
    transaction = journal.get("transaction")
    if (
        not isinstance(transaction, str)
        or len(transaction) > _MAX_TRANSACTION_STRING
        or _TRANSACTION_RE.fullmatch(transaction) is None
    ):
        raise CommandCustodyConflict("lifecycle journal transaction is invalid")
    command_id = _validate_command_id(journal.get("command_id"), label="lifecycle journal command id")
    operation = journal.get("operation")
    if operation not in DISPOSITION_STAGES:
        raise CommandCustodyConflict("lifecycle journal operation is invalid")
    if journal.get("source_stage") not in {"pending", "approved"} or journal.get("target_stage") != operation:
        raise CommandCustodyConflict("lifecycle journal stage role mapping is invalid")
    if journal.get("source_status") not in (PENDING_STATUS, AUTONOMOUS_PENDING_STATUS, APPROVED_STATUS):
        raise CommandCustodyConflict("lifecycle journal source status is invalid")
    if journal.get("target_status") != DISPOSITION_STATUSES[operation]:
        raise CommandCustodyConflict("lifecycle journal target status is invalid")
    phase = journal.get("phase")
    if phase not in _JOURNAL_PHASES:
        raise CommandCustodyConflict("lifecycle journal phase is invalid")
    created_at = journal.get("created_at")
    if not isinstance(created_at, str) or not created_at or len(created_at) > 80:
        raise CommandCustodyConflict("lifecycle journal timestamp is invalid")
    try:
        parsed_time = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CommandCustodyConflict("lifecycle journal timestamp is invalid") from exc
    if parsed_time.tzinfo is None:
        raise CommandCustodyConflict("lifecycle journal timestamp has no timezone")
    original_mtime = journal.get("original_mtime_ns")
    if (
        not isinstance(original_mtime, int)
        or isinstance(original_mtime, bool)
        or original_mtime < 0
        or original_mtime > 2**64 - 1
    ):
        raise CommandCustodyConflict("lifecycle journal original mtime is invalid")
    for field in ("original_sha256", "final_sha256", "journal_sha256"):
        value = journal.get(field)
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise CommandCustodyConflict(f"lifecycle journal {field} is invalid")
    recorded_journal_identity = _identity_from_value(
        journal.get("journal_identity"),
        label="journal identity",
    )
    if recorded_journal_identity != identity:
        raise CommandCustodyConflict("lifecycle journal identity does not match its pathname")
    if _journal_digest(journal) != journal["journal_sha256"]:
        raise CommandCustodyConflict("lifecycle journal self-hash is invalid")

    source = _path_from_relative(bridge_dir, journal.get("source"))
    target = _path_from_relative(bridge_dir, journal.get("target"))
    original_payload = _path_from_relative(bridge_dir, journal.get("original_payload"))
    final_payload = _path_from_relative(bridge_dir, journal.get("final_payload"))
    bridge = _absolute(bridge_dir)
    expected_source = bridge / "commands" / "pending" / f"{command_id}.json"
    expected_target = bridge / "commands" / str(operation) / f"{command_id}.json"
    recovery = bridge / "commands" / LIFECYCLE_RECOVERY_DIR_NAME
    expected_original, expected_final, expected_journal = _recovery_payload_paths(
        recovery,
        command_id,
        transaction,
    )
    if not _same_path(source, expected_source) or not _same_path(target, expected_target):
        raise CommandCustodyConflict("lifecycle journal source/destination roles are not canonical")
    if not _same_path(original_payload, expected_original) or not _same_path(final_payload, expected_final):
        raise CommandCustodyConflict("lifecycle journal payload path is not transaction-owned")
    if not _same_path(path, expected_journal):
        raise CommandCustodyConflict("lifecycle journal filename is not transaction-owned")
    _identity_from_value(journal.get("original_identity"), label="original identity")
    _identity_from_value(journal.get("original_payload_identity"), label="original payload identity")
    _identity_from_value(journal.get("final_payload_identity"), label="final payload identity")
    return journal


def _load_journals(bridge_dir: pathlib.Path) -> List[_JournalRecord]:
    layout = _ensure_layout(bridge_dir)
    recovery = layout[LIFECYCLE_RECOVERY_DIR_NAME]
    entries = _directory_entries(recovery, label="lifecycle recovery directory")
    records: List[_JournalRecord] = []
    expected_names: set[str] = set()
    for path, _metadata in entries:
        if not path.name.endswith(".lifecycle.json"):
            if path.name.endswith(".original.bytes") or path.name.endswith(".final.bytes"):
                continue
            raise CommandCustodyUnknown(f"orphan lifecycle payload or temporary: {path}")
        try:
            raw, identity, _mtime_ns_value = read_stable_bytes(
                path,
                label=f"lifecycle journal {path.name}",
            )
            journal = _validate_journal_record(bridge_dir, path, raw, identity)
        except CommandCustodyError as exc:
            if isinstance(exc, CommandCustodyUnknown):
                raise
            raise CommandCustodyUnknown(f"lifecycle journal cannot be trusted: {path}") from exc
        records.append(_JournalRecord(path=path, raw=raw, identity=identity, journal=journal))
        expected_names.add(path.name)
        expected_names.add(pathlib.PurePosixPath(str(journal["original_payload"])).name)
        expected_names.add(pathlib.PurePosixPath(str(journal["final_payload"])).name)
    for path, _metadata in entries:
        if path.name not in expected_names:
            raise CommandCustodyUnknown(f"unreferenced lifecycle recovery evidence: {path}")
    by_command: Dict[str, List[_JournalRecord]] = {}
    for record in records:
        by_command.setdefault(str(record.journal["command_id"]), []).append(record)
    if any(len(items) != 1 for items in by_command.values()):
        raise CommandCustodyUnknown("duplicate lifecycle journals claim one command id")
    return records


def _read_owned_payload(
    path: pathlib.Path,
    *,
    expected_identity: Tuple[int, int],
    expected_hash: str,
    label: str,
    allow_missing: bool = False,
) -> Optional[bytes]:
    metadata = _lstat_safe(path, label=label, allow_missing=True)
    if metadata is None:
        if allow_missing:
            return None
        raise CommandCustodyUnknown(f"{label} is missing")
    if _identity(metadata) != expected_identity:
        raise CommandCustodyUnknown(f"{label} identity does not match its journal")
    try:
        raw, identity, _mtime = read_stable_bytes(path, label=label)
    except CommandCustodyError as exc:
        raise CommandCustodyUnknown(f"{label} could not be revalidated") from exc
    if identity != expected_identity or sha256_bytes(raw) != expected_hash:
        raise CommandCustodyUnknown(f"{label} bytes or identity do not match its journal")
    return raw


def _unlink_owned(
    path: pathlib.Path,
    *,
    expected_identity: Tuple[int, int],
    expected_bytes: bytes,
    label: str,
) -> None:
    metadata = _lstat_safe(path, label=label, allow_missing=True)
    if metadata is None:
        raise CommandCustodyUnknown(f"{label} disappeared before cleanup")
    if _identity(metadata) != expected_identity:
        raise CommandCustodyUnknown(f"{label} replacement identity is not owned")
    try:
        raw, identity, mtime = read_stable_bytes(path, label=label)
    except CommandCustodyError as exc:
        raise CommandCustodyUnknown(f"{label} could not be revalidated for cleanup") from exc
    if identity != expected_identity or raw != expected_bytes:
        raise CommandCustodyUnknown(f"{label} bytes or identity changed before cleanup")
    try:
        raw_again, identity_again, mtime_again = read_stable_bytes(path, label=label)
        named_again = _validate_regular_queue_file(path, label=label)
    except CommandCustodyError as exc:
        raise CommandCustodyUnknown(f"{label} could not be revalidated at the cleanup boundary") from exc
    if (
        identity_again != expected_identity
        or raw_again != expected_bytes
        or mtime_again != mtime
        or _identity(named_again) != expected_identity
    ):
        raise CommandCustodyUnknown(f"{label} replacement or byte change was observed at cleanup")
    try:
        path.unlink()
    except OSError as exc:
        raise CommandCustodyUnknown(f"{label} cleanup crossed an uncertain boundary") from exc
    if _lstat_safe(path, label=label, allow_missing=True) is not None:
        raise CommandCustodyUnknown(f"{label} remained after cleanup")


def _rewrite_journal_phase(record: _JournalRecord, phase: str) -> _JournalRecord:
    if phase not in _JOURNAL_PHASES:
        raise CommandCustodyError(f"invalid lifecycle journal phase: {phase}")
    journal = dict(record.journal)
    journal["phase"] = phase
    raw = _journal_bytes(journal)
    _rewrite_file_exact(
        record.path,
        raw,
        expected_identity=record.identity,
        label=f"lifecycle journal {record.path.name}",
    )
    return _JournalRecord(path=record.path, raw=raw, identity=record.identity, journal=journal)


def _recover_one_journal_locked(bridge_dir: pathlib.Path, record: _JournalRecord) -> str:
    journal = record.journal
    command_id = str(journal["command_id"])
    operation = str(journal["operation"])
    original_identity = _identity_from_value(journal["original_identity"], label="original identity")
    original_payload_identity = _identity_from_value(
        journal["original_payload_identity"],
        label="original payload identity",
    )
    final_payload_identity = _identity_from_value(
        journal["final_payload_identity"],
        label="final payload identity",
    )
    source = _path_from_relative(bridge_dir, journal["source"])
    target = _path_from_relative(bridge_dir, journal["target"])
    original_path = _path_from_relative(bridge_dir, journal["original_payload"])
    final_path = _path_from_relative(bridge_dir, journal["final_payload"])
    original_hash = str(journal["original_sha256"])
    final_hash = str(journal["final_sha256"])
    original_bytes = _read_owned_payload(
        original_path,
        expected_identity=original_payload_identity,
        expected_hash=original_hash,
        label="lifecycle original payload",
        allow_missing=journal["phase"] in {"restoring", "completing"},
    )
    final_bytes = _read_owned_payload(
        final_path,
        expected_identity=final_payload_identity,
        expected_hash=final_hash,
        label="lifecycle final payload",
        allow_missing=True,
    )
    if original_bytes is not None:
        original_command = _validate_command_envelope(
            _strict_json_loads(original_bytes, label="lifecycle original payload"),
            expected_id=command_id,
            expected_stage="pending",
            label="lifecycle original payload",
        )
        if original_command.get("status") != journal["source_status"]:
            raise CommandCustodyUnknown("lifecycle original payload status is not journal-bound")
    if final_bytes is not None:
        final_command = _validate_command_envelope(
            _strict_json_loads(final_bytes, label="lifecycle final payload"),
            expected_id=command_id,
            expected_status=str(journal["target_status"]),
            label="lifecycle final payload",
        )
        expected_schema = AUTONOMOUS_COMMAND_SCHEMA if journal["source_status"] == AUTONOMOUS_PENDING_STATUS else COMMAND_SCHEMA
        if final_command.get("schema") != expected_schema:
            raise CommandCustodyUnknown("lifecycle final payload authorization version is not journal-bound")

    locations = _scan_command_locations(bridge_dir)
    owned_locations = [item for item in locations if item.command.get("id") == command_id]
    expected_source = _absolute(source)
    expected_target = _absolute(target)
    if any(
        not _same_path(item.path, expected_source)
        and not _same_path(item.path, expected_target)
        for item in owned_locations
    ):
        raise CommandCustodyUnknown("lifecycle command exists outside journal source/destination roles")
    source_location = next(
        (item for item in owned_locations if _same_path(item.path, expected_source)),
        None,
    )
    target_location = next(
        (item for item in owned_locations if _same_path(item.path, expected_target)),
        None,
    )
    if source_location is not None:
        if (
            source_location.identity != original_identity
            or sha256_bytes(source_location.payload) != original_hash
            or (original_bytes is not None and source_location.payload != original_bytes)
        ):
            raise CommandCustodyUnknown("pending source is not the recorded original owner")
    if target_location is not None:
        expected_schema = AUTONOMOUS_COMMAND_SCHEMA if journal["source_status"] == AUTONOMOUS_PENDING_STATUS else COMMAND_SCHEMA
        if target_location.command.get("schema") != expected_schema:
            raise CommandCustodyUnknown("terminal destination authorization version is not journal-bound")
        if (
            target_location.identity != final_payload_identity
            or sha256_bytes(target_location.payload) != final_hash
            or (final_bytes is not None and target_location.payload != final_bytes)
        ):
            raise CommandCustodyUnknown("terminal destination is not the recorded terminal owner")
    phase = str(journal["phase"])
    if source_location is not None and target_location is None:
        if phase not in {"staged", "restoring"}:
            raise CommandCustodyUnknown("published lifecycle journal lost its terminal destination")
        if phase == "staged":
            record = _rewrite_journal_phase(record, "restoring")
        if original_bytes is None or final_bytes is None:
            raise CommandCustodyUnknown("restoring lifecycle journal lost its source evidence")
        if final_bytes is not None:
            _unlink_owned(
                final_path,
                expected_identity=final_payload_identity,
                expected_bytes=final_bytes,
                label="lifecycle final payload",
            )
        _unlink_owned(
            original_path,
            expected_identity=original_payload_identity,
            expected_bytes=original_bytes,
            label="lifecycle original payload",
        )
        _unlink_owned(
            record.path,
            expected_identity=record.identity,
            expected_bytes=record.raw,
            label=f"lifecycle journal {record.path.name}",
        )
        return "restored_pending"

    if source_location is not None and target_location is not None:
        if final_bytes is not None and _lstat_safe(
            final_path,
            label="lifecycle final payload",
            allow_missing=True,
        ) is not None:
            raise CommandCustodyUnknown("terminal destination coexists with replayable final payload")
        if phase in {"staged", "published"}:
            record = _rewrite_journal_phase(record, "completing")
        _unlink_owned(
            source,
            expected_identity=original_identity,
            expected_bytes=original_bytes if original_bytes is not None else source_location.payload,
            label="recoverable pending source",
        )
        if original_bytes is not None:
            _unlink_owned(
                original_path,
                expected_identity=original_payload_identity,
                expected_bytes=original_bytes,
                label="lifecycle original payload",
            )
        _unlink_owned(
            record.path,
            expected_identity=record.identity,
            expected_bytes=record.raw,
            label=f"lifecycle journal {record.path.name}",
        )
        return "completed"

    if source_location is None and target_location is not None:
        if phase != "completing":
            raise CommandCustodyUnknown("terminal destination exists without completed source cleanup phase")
        if _lstat_safe(final_path, label="lifecycle final payload", allow_missing=True) is not None:
            raise CommandCustodyUnknown("terminal destination coexists with replayable final payload")
        if original_bytes is not None:
            _unlink_owned(
                original_path,
                expected_identity=original_payload_identity,
                expected_bytes=original_bytes,
                label="lifecycle original payload",
            )
        _unlink_owned(
            record.path,
            expected_identity=record.identity,
            expected_bytes=record.raw,
            label=f"lifecycle journal {record.path.name}",
        )
        return "completed"

    raise CommandCustodyUnknown("lifecycle journal has no provable source or terminal owner")


def _recover_lifecycle_transactions_locked(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    records = _load_journals(bridge_dir)
    results: List[Dict[str, str]] = []
    for record in records:
        classification = _recover_one_journal_locked(bridge_dir, record)
        results.append({"journal": str(record.path), "classification": classification})
    locations = _scan_command_locations(bridge_dir)
    _assert_no_duplicate_locations(locations)
    remaining = _load_journals(bridge_dir)
    return {
        "schema": LIFECYCLE_JOURNAL_SCHEMA,
        "journals": results,
        "unknown": 0,
        "active": len(remaining),
    }


def _strict_locked_preflight(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    recovery = _recover_lifecycle_transactions_locked(bridge_dir)
    locations = _scan_command_locations(bridge_dir)
    _assert_no_duplicate_locations(locations)
    return recovery


def recover_lifecycle_transactions(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    """Recover only provable disposition outcomes under the actual OS lock."""

    with lifecycle_custody(bridge_dir):
        return _strict_locked_preflight(bridge_dir)


def lifecycle_recovery_blockers(bridge_dir: pathlib.Path) -> List[str]:
    """List recovery evidence without following unsafe paths."""

    bridge = _absolute(pathlib.Path(bridge_dir))
    recovery = bridge / "commands" / LIFECYCLE_RECOVERY_DIR_NAME
    if _lstat_safe(bridge, label="Bridge directory", allow_missing=True) is None:
        return []
    if _lstat_safe(recovery.parent, label="commands directory", allow_missing=True) is None:
        return []
    if _lstat_safe(recovery, label="lifecycle recovery directory", allow_missing=True) is None:
        return []
    return [
        path.name
        for path, _metadata in _directory_entries(recovery, label="lifecycle recovery directory")
    ]


def approval_recovery_blockers(bridge_dir: pathlib.Path) -> List[str]:
    """Retain approval ambiguity and private claims as no-replay latches."""

    bridge = _absolute(pathlib.Path(bridge_dir))
    commands = bridge / "commands"
    if _lstat_safe(commands, label="commands directory", allow_missing=True) is None:
        return []
    blockers = [
        path.name
        for path, _metadata in _directory_entries(commands, label="commands directory")
        if path.name.startswith(".approval-") and path.name.endswith(".lock")
    ]
    recovery = commands / "approval-recovery"
    if _lstat_safe(recovery, label="approval recovery directory", allow_missing=True) is not None:
        blockers.extend(
            f"approval-recovery/{path.name}"
            for path, _metadata in _directory_entries(recovery, label="approval recovery directory")
        )
    return sorted(blockers)


def validation_control_blockers(bridge_dir: pathlib.Path) -> List[str]:
    """Treat nonterminal validation controls as competing game ownership."""

    root = _absolute(pathlib.Path(bridge_dir)) / "validation-controls"
    if _lstat_safe(root, label="validation controls directory", allow_missing=True) is None:
        return []
    blockers: List[str] = []
    for stage in ("approved", "inflight"):
        directory = root / stage
        if _lstat_safe(directory, label=f"validation controls {stage}", allow_missing=True) is None:
            continue
        blockers.extend(
            f"validation-controls/{stage}/{path.name}"
            for path, _metadata in _directory_entries(directory, label=f"validation controls {stage}")
        )
    return sorted(blockers)


__all__ = [
    "COMMAND_STAGES",
    "CommandCustodyConflict",
    "CommandCustodyError",
    "CommandCustodyTimeout",
    "CommandCustodyUnknown",
    "DISPOSITION_STAGES",
    "PendingEnvelopeObservation",
    "commands_root_for",
    "approval_recovery_blockers",
    "lifecycle_custody",
    "lifecycle_lock_path",
    "lifecycle_recovery_blockers",
    "lifecycle_recovery_dir",
    "publish_pending_command",
    "queue_stage_path",
    "read_stable_bytes",
    "observe_pending_envelope",
    "recover_lifecycle_transactions",
    "sha256_bytes",
    "transition_pending_envelope",
    "validation_control_blockers",
]
