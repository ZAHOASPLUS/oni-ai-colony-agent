"""Immutable decision-frame archives and coherent operator command bindings.

The root ``decision-frame.json`` is a moving generation commit marker.  A
mutating command must instead retain and later approve against the one archived
frame named by its ``source_frame_id``.  This module owns that narrow storage
contract so callers never turn an untrusted frame id into an arbitrary path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile
from typing import Any, Dict, Mapping

from agent.action_policy import project_action_policy
from agent.canonical_hash import (
    CanonicalHashError,
    canonical_bytes,
    canonical_hash,
    loads_strict,
)
from agent.decision import DecisionFrameError, validate_decision_frame
from agent.state_quality import canonical_state_hash


ARCHIVE_DIR_NAME = "decision-frames"
MAX_UNREFERENCED_DECISION_FRAMES = 128
NONTERMINAL_COMMAND_STAGES = (
    "pending",
    "approving",
    "approved",
    "executing",
    "confirming",
)
OPERATOR_MUTATING_MODES = frozenset({"runtime_validation", "planned"})

_FRAME_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARCHIVE_FILE_RE = re.compile(r"^sha256-([0-9a-f]{64})\.json$")


class DecisionFrameArchiveError(RuntimeError):
    """Raised when an immutable frame or current decision bundle is unsafe."""


@dataclasses.dataclass(frozen=True)
class CurrentDecisionContext:
    bridge_dir: pathlib.Path
    frame: Dict[str, Any]
    state: Dict[str, Any]
    policy: Dict[str, Any]
    frame_id: str
    policy_hash: str
    archive_path: pathlib.Path
    file_hashes: Dict[pathlib.Path, str]


def validate_frame_id(value: Any) -> str:
    if not isinstance(value, str) or _FRAME_ID_RE.fullmatch(value) is None:
        raise DecisionFrameArchiveError("decision frame id is missing or invalid")
    return value


def decision_frame_archive_path(bridge_dir: pathlib.Path, frame_id: Any) -> pathlib.Path:
    """Return the sole allowed archive path for a validated frame id.

    The colon in the external ``sha256:...`` envelope is deliberately replaced
    by a fixed dash.  The only variable path component is the validated lower-
    case hexadecimal digest, so this is safe on Windows and cannot traverse.
    """

    validated = validate_frame_id(frame_id)
    digest = validated[len("sha256:") :]
    return pathlib.Path(bridge_dir) / ARCHIVE_DIR_NAME / f"sha256-{digest}.json"


def frame_id_from_archive_name(name: str) -> str | None:
    match = _ARCHIVE_FILE_RE.fullmatch(name)
    return f"sha256:{match.group(1)}" if match is not None else None


def _strict_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_bytes(left) == canonical_bytes(right)
    except (CanonicalHashError, TypeError, ValueError, OverflowError):
        return False


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _read_regular_bytes(path: pathlib.Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DecisionFrameArchiveError(f"{label} is missing or unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DecisionFrameArchiveError(f"{label} must be a regular non-link file")
    if int(metadata.st_nlink) != 1:
        raise DecisionFrameArchiveError(f"{label} must have exactly one filesystem link")

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        expected_identity = (int(metadata.st_dev), int(metadata.st_ino))
        opened_identity = (int(opened.st_dev), int(opened.st_ino))
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
            or opened_identity != expected_identity
        ):
            raise DecisionFrameArchiveError(
                f"{label} changed identity or link count while it was opened"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (int(after.st_dev), int(after.st_ino)) != opened_identity
            or int(after.st_nlink) != 1
            or int(after.st_size) != sum(len(chunk) for chunk in chunks)
        ):
            raise DecisionFrameArchiveError(f"{label} changed while it was read")
        try:
            named_after = path.lstat()
        except OSError as exc:
            raise DecisionFrameArchiveError(
                f"{label} path changed while it was read"
            ) from exc
        if (
            stat.S_ISLNK(named_after.st_mode)
            or not stat.S_ISREG(named_after.st_mode)
            or int(named_after.st_nlink) != 1
            or (int(named_after.st_dev), int(named_after.st_ino)) != opened_identity
        ):
            raise DecisionFrameArchiveError(
                f"{label} path changed identity or link count while it was read"
            )
        return b"".join(chunks)
    except DecisionFrameArchiveError:
        raise
    except OSError as exc:
        raise DecisionFrameArchiveError(f"{label} is missing or unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_object_bytes(payload: bytes, label: str) -> Dict[str, Any]:
    try:
        value = loads_strict(payload.decode("utf-8-sig", errors="strict"))
    except (UnicodeError, CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise DecisionFrameArchiveError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise DecisionFrameArchiveError(f"{label} must contain a JSON object")
    return value


def _read_object(path: pathlib.Path, label: str) -> tuple[Dict[str, Any], bytes]:
    payload = _read_regular_bytes(path, label)
    return _load_object_bytes(payload, label), payload


def _validate_frame(frame: Mapping[str, Any], *, expected_id: str | None = None) -> str:
    try:
        validate_decision_frame(frame)
    except (DecisionFrameError, TypeError, ValueError, OverflowError) as exc:
        raise DecisionFrameArchiveError(f"decision frame is invalid: {exc}") from exc
    frame_id = validate_frame_id(frame.get("frame_id"))
    if expected_id is not None and frame_id != expected_id:
        raise DecisionFrameArchiveError("archived decision frame id does not match its requested id")
    return frame_id


def _canonical_json_document(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (UnicodeError, TypeError, ValueError, OverflowError) as exc:
        raise DecisionFrameArchiveError("decision frame cannot be encoded as canonical JSON") from exc


def persist_decision_frame_archive(
    bridge_dir: pathlib.Path,
    frame: Mapping[str, Any],
) -> pathlib.Path:
    """Publish one immutable archive without ever replacing an existing id."""

    frame_id = _validate_frame(frame)
    target = decision_frame_archive_path(bridge_dir, frame_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() or target.is_symlink():
        existing, _ = _read_object(target, "archived decision frame")
        _validate_frame(existing, expected_id=frame_id)
        if not _strict_equal(existing, frame):
            raise DecisionFrameArchiveError("existing decision frame archive payload differs")
        return target

    payload = _canonical_json_document(frame)
    temporary: pathlib.Path | None = None
    cleanup_error: OSError | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            delete=False,
            dir=str(target.parent),
            suffix=".tmp",
        ) as handle:
            temporary = pathlib.Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A same-directory hard link publishes the fully flushed file while
            # preserving create-if-absent semantics.  Unlike os.replace, this
            # can never overwrite a previously committed immutable frame.
            os.link(temporary, target)
        except FileExistsError:
            existing, _ = _read_object(target, "archived decision frame")
            _validate_frame(existing, expected_id=frame_id)
            if not _strict_equal(existing, frame):
                raise DecisionFrameArchiveError("concurrent decision frame archive payload differs")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = exc
    if cleanup_error is not None:
        raise DecisionFrameArchiveError(
            "decision frame archive temporary hard-link alias could not be removed"
        ) from cleanup_error
    # Re-open through the hardened reader after the temporary alias is gone.
    # This proves the published inode has a single link and the expected bytes.
    committed, _ = _read_object(target, "archived decision frame")
    _validate_frame(committed, expected_id=frame_id)
    if not _strict_equal(committed, frame):
        raise DecisionFrameArchiveError("committed decision frame archive payload differs")
    return target


def load_archived_decision_frame(
    bridge_dir: pathlib.Path,
    frame_id: Any,
) -> tuple[Dict[str, Any], pathlib.Path, str]:
    """Load only the archive derived from ``frame_id``; there is no fallback."""

    validated = validate_frame_id(frame_id)
    path = decision_frame_archive_path(bridge_dir, validated)
    frame, payload = _read_object(path, "archived decision frame")
    _validate_frame(frame, expected_id=validated)
    return frame, path, _sha256_bytes(payload)


def _expected_frame_source(state: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        "snapshot_id": state.get("snapshot_id"),
        "seq": state.get("seq"),
        "snapshot_seq": state.get("snapshot_seq"),
        "ts": state.get("ts"),
        "state_hash": canonical_state_hash(dict(state)),
    }


def _expected_policy_projection(policy: Mapping[str, Any]) -> Dict[str, Any]:
    return project_action_policy(policy)


def _validate_operator_frame(
    frame: Mapping[str, Any],
    state: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    mode: str,
) -> None:
    if mode not in OPERATOR_MUTATING_MODES:
        raise DecisionFrameArchiveError("operator decision context mode is unsupported")
    trust = frame.get("trust") if isinstance(frame.get("trust"), Mapping) else {}
    readiness = frame.get("readiness") if isinstance(frame.get("readiness"), Mapping) else {}
    propose = readiness.get("propose") if isinstance(readiness.get("propose"), Mapping) else {}
    if trust.get("status") != "trusted":
        raise DecisionFrameArchiveError("current decision frame is not trusted")
    if dict(propose) not in (
        {"ready": True, "blockers": []},
        {"ready": False, "blockers": ["SUGGESTIONS_DISABLED"]},
    ):
        raise DecisionFrameArchiveError(
            "operator frame proposal blockers must be empty or exactly SUGGESTIONS_DISABLED"
        )
    if not _strict_equal(frame.get("source"), _expected_frame_source(state)):
        raise DecisionFrameArchiveError(
            "current state does not exactly match the decision frame source"
        )
    projections = frame.get("projections") if isinstance(frame.get("projections"), Mapping) else {}
    if not _strict_equal(
        projections.get("policy"),
        _expected_policy_projection(policy),
    ):
        raise DecisionFrameArchiveError(
            "current action policy does not match the decision frame policy projection"
        )


def load_current_decision_context(
    bridge_dir: pathlib.Path,
    *,
    mode: str,
) -> CurrentDecisionContext:
    """Optimistically read one coherent root frame, state, policy, and archive."""

    root = pathlib.Path(bridge_dir).expanduser().resolve()
    frame_path = root / "decision-frame.json"
    state_path = root / "latest_state.json"
    policy_path = root / "action-policy.json"

    frame, frame_payload = _read_object(frame_path, "current decision frame")
    frame_id = _validate_frame(frame)
    state, state_payload = _read_object(state_path, "current state")
    policy, policy_payload = _read_object(policy_path, "current action policy")
    archived, archive_path, archive_hash = load_archived_decision_frame(root, frame_id)
    if not _strict_equal(archived, frame):
        raise DecisionFrameArchiveError(
            "current decision frame differs from its immutable archive"
        )
    _validate_operator_frame(frame, state, policy, mode=mode)
    try:
        policy_hash = canonical_hash(policy)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise DecisionFrameArchiveError("current action policy is not canonical") from exc

    file_hashes = {
        frame_path: _sha256_bytes(frame_payload),
        state_path: _sha256_bytes(state_payload),
        policy_path: _sha256_bytes(policy_payload),
        archive_path: archive_hash,
    }
    context = CurrentDecisionContext(
        bridge_dir=root,
        frame=frame,
        state=state,
        policy=policy,
        frame_id=frame_id,
        policy_hash=policy_hash,
        archive_path=archive_path,
        file_hashes=file_hashes,
    )
    assert_current_decision_context(context)
    return context


def assert_current_decision_context(context: CurrentDecisionContext) -> None:
    """Reject any change to the bundle observed by a prepared command."""

    for path, expected in context.file_hashes.items():
        payload = _read_regular_bytes(path, path.name)
        if _sha256_bytes(payload) != expected:
            raise DecisionFrameArchiveError(
                "decision frame, state, policy, or archive changed during command preparation"
            )


def validate_command_policy_binding(
    command: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> str:
    """Require the command's immutable source to name the current policy hash."""

    try:
        expected = canonical_hash(policy)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise DecisionFrameArchiveError("current action policy is not canonical") from exc
    source = command.get("source")
    if not isinstance(source, Mapping):
        raise DecisionFrameArchiveError("mutating command source is missing")
    bound = source.get("policy_hash")
    if not isinstance(bound, str) or _FRAME_ID_RE.fullmatch(bound) is None:
        raise DecisionFrameArchiveError("mutating command source policy_hash is missing or invalid")
    if bound != expected:
        raise DecisionFrameArchiveError("mutating command policy binding is stale")
    return expected


def referenced_nonterminal_frame_ids(bridge_dir: pathlib.Path) -> set[str]:
    """Collect valid frame ids used by every nonterminal command queue stage."""

    result: set[str] = set()
    commands_root = pathlib.Path(bridge_dir) / "commands"
    for stage in NONTERMINAL_COMMAND_STAGES:
        directory = commands_root / stage
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                value, _ = _read_object(path, f"{stage} command")
            except DecisionFrameArchiveError:
                continue
            frame_id = value.get("source_frame_id")
            if isinstance(frame_id, str) and _FRAME_ID_RE.fullmatch(frame_id) is not None:
                result.add(frame_id)
    return result


def prune_decision_frame_archives(
    bridge_dir: pathlib.Path,
    *,
    current_frame_id: str | None = None,
    max_unreferenced: int = MAX_UNREFERENCED_DECISION_FRAMES,
) -> Dict[str, Any]:
    """Bound old archives while preserving all active command references."""

    if isinstance(max_unreferenced, bool) or not isinstance(max_unreferenced, int) or max_unreferenced < 0:
        raise DecisionFrameArchiveError("archive retention limit is invalid")
    preserved = referenced_nonterminal_frame_ids(bridge_dir)
    if current_frame_id is not None:
        preserved.add(validate_frame_id(current_frame_id))

    archive_dir = pathlib.Path(bridge_dir) / ARCHIVE_DIR_NAME
    if not archive_dir.is_dir():
        return {
            "candidate_count": 0,
            "preserved_count": 0,
            "unreferenced_kept": 0,
            "pruned_count": 0,
        }

    candidates: list[tuple[int, str, pathlib.Path, str]] = []
    for path in archive_dir.iterdir():
        frame_id = frame_id_from_archive_name(path.name)
        if frame_id is None:
            continue
        try:
            modified = path.lstat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified, path.name, path, frame_id))

    unreferenced = [item for item in candidates if item[3] not in preserved]
    unreferenced.sort(key=lambda item: (item[0], item[1]), reverse=True)
    to_prune = unreferenced[max_unreferenced:]
    pruned = 0
    for _modified, _name, path, _frame_id in to_prune:
        try:
            path.unlink()
            pruned += 1
        except FileNotFoundError:
            continue
    return {
        "candidate_count": len(candidates),
        "preserved_count": sum(1 for item in candidates if item[3] in preserved),
        "unreferenced_kept": min(len(unreferenced), max_unreferenced),
        "pruned_count": pruned,
    }


__all__ = [
    "ARCHIVE_DIR_NAME",
    "CurrentDecisionContext",
    "DecisionFrameArchiveError",
    "MAX_UNREFERENCED_DECISION_FRAMES",
    "NONTERMINAL_COMMAND_STAGES",
    "assert_current_decision_context",
    "decision_frame_archive_path",
    "frame_id_from_archive_name",
    "load_archived_decision_frame",
    "load_current_decision_context",
    "persist_decision_frame_archive",
    "prune_decision_frame_archives",
    "referenced_nonterminal_frame_ids",
    "validate_command_policy_binding",
    "validate_frame_id",
]
