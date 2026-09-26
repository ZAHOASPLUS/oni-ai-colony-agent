#!/usr/bin/env python3
"""Atomically claim one pending command and publish its approved payload.

The executor never scans ``commands/approving``.  Moving the pending file there
first therefore removes the copy-then-delete window where both pending and
approved copies could be visible while the game was polling the queue.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import time
from typing import Any, Dict


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.decision_frame_archive import (  # noqa: E402
    DecisionFrameArchiveError,
    decision_frame_archive_path,
    load_archived_decision_frame,
    validate_command_policy_binding,
)
from agent.action_outcomes import command_intent_hash  # noqa: E402
from agent.action_policy import action_policy_is_valid  # noqa: E402
from agent import command_custody  # noqa: E402


class ApprovalCommitError(RuntimeError):
    pass


class ApprovalOutcomeUnknown(ApprovalCommitError):
    """Raised when an approval may already have crossed an executor boundary."""


_QUEUE_STAGES = (
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
_EXECUTOR_OR_TERMINAL_STAGES = (
    "executing",
    "confirming",
    "done",
    "failed",
    "rejected",
    "quarantine",
)
_APPROVAL_RECOVERY_DIR_NAME = "approval-recovery"
_REPARSE_POINT = 0x0400


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _load_object(path: pathlib.Path) -> Dict[str, Any]:
    try:
        raw, _identity, _mtime_ns = command_custody.read_stable_bytes(
            path,
            label=f"approval JSON {path.name}",
        )
        value = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_constant,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        command_custody.CommandCustodyError,
    ) as exc:
        raise ApprovalCommitError(f"invalid JSON object: {path.name}") from exc
    if not isinstance(value, dict):
        raise ApprovalCommitError(f"JSON payload is not an object: {path.name}")
    return value


def _sha256(path: pathlib.Path) -> str:
    try:
        raw, _identity, _mtime_ns = command_custody.read_stable_bytes(
            path,
            label=f"approval file {path.name}",
        )
    except command_custody.CommandCustodyError as exc:
        raise ApprovalCommitError(f"approval file is not stable: {path.name}") from exc
    return hashlib.sha256(raw).hexdigest().upper()


def _require_hash(path: pathlib.Path, expected: str | None, label: str) -> None:
    if expected is None:
        return
    if _sha256(path) != expected.upper():
        raise ApprovalCommitError(f"{label} changed before atomic approval claim")


def _recovery_dir_is_owned(
    commands_root: pathlib.Path,
    recovery_dir: pathlib.Path,
    identity: tuple[int, int] | None,
) -> bool:
    if identity is None:
        return False
    try:
        if not _same_path(recovery_dir.parent, commands_root):
            return False
    except ValueError:
        return False
    try:
        metadata = _safe_directory_metadata(recovery_dir, label="approval recovery directory")
    except ApprovalCommitError:
        return False
    return (int(metadata.st_dev), int(metadata.st_ino)) == identity


def _same_path(left: pathlib.Path, right: pathlib.Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _parse_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ApprovalCommitError(f"{label} is missing or invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalCommitError(f"{label} is missing or invalid") from exc
    if parsed.tzinfo is None:
        raise ApprovalCommitError(f"{label} must include a UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def _require_unexpired(command: Dict[str, Any]) -> None:
    expires_at = _parse_timestamp(command.get("expires_at"), "expires_at")
    approved_at = _parse_timestamp(command.get("approved_at"), "approved_at")
    if approved_at >= expires_at:
        raise ApprovalCommitError("command approval timestamp is not before expiry")
    if dt.datetime.now(dt.timezone.utc) >= expires_at:
        raise ApprovalCommitError("command expired during approval commit")


def _validate_payloads(
    pending: Dict[str, Any],
    approved: Dict[str, Any],
    policy_path: pathlib.Path,
    frame_path: pathlib.Path | None,
) -> Dict[str, Any]:
    command_id = pending.get("id")
    action = pending.get("action")
    if not isinstance(command_id, str) or not command_id:
        raise ApprovalCommitError("pending command id is invalid")
    if approved.get("id") != command_id or approved.get("action") != action:
        raise ApprovalCommitError("approved payload identity differs from pending command")
    if pending.get("status") != "pending_human_approval" or approved.get("status") != "approved":
        raise ApprovalCommitError("approval state transition is invalid")
    if approved.get("approved_by") != "local_human" or not isinstance(approved.get("approved_at"), str):
        raise ApprovalCommitError("approved payload lacks local human approval metadata")
    _require_unexpired(approved)

    policy = _load_object(policy_path)
    if not action_policy_is_valid(policy) or policy.get("actions_enabled") is not True:
        raise ApprovalCommitError("action policy is invalid or disabled")
    if action not in list(policy.get("allowed_actions") or []):
        raise ApprovalCommitError("command action is not allowed by policy")
    if pending.get("save_id_hash") not in list(policy.get("allowed_save_hashes") or []):
        raise ApprovalCommitError("command save is not allowed by policy")

    mutating_v3 = pending.get("schema") == "oni.command.v3" and action not in {"noop", "focus_note"}
    if mutating_v3:
        if frame_path is None:
            raise ApprovalCommitError("mutating approval requires a decision frame")
        from agent.approval_receipt import ApprovalReceiptError, validate_approval_receipt

        try:
            if command_intent_hash(pending) != command_intent_hash(approved):
                raise ApprovalCommitError(
                    "approved payload intent differs from the claimed pending command"
                )
            validate_command_policy_binding(pending, policy)
            frame, expected_path, _archive_hash = load_archived_decision_frame(
                policy_path.parent,
                pending.get("source_frame_id"),
            )
        except DecisionFrameArchiveError as exc:
            raise ApprovalCommitError(str(exc)) from exc
        if not _same_path(frame_path, expected_path):
            raise ApprovalCommitError(
                "approval frame path does not match command source_frame_id archive"
            )
        try:
            validate_approval_receipt(approved, policy, frame)
        except ApprovalReceiptError as exc:
            raise ApprovalCommitError(f"approval receipt is invalid: {exc}") from exc
        return approved

    if pending.get("schema") not in {"oni.command.v2", "oni.command.v3"} or action not in {
        "noop",
        "focus_note",
    }:
        raise ApprovalCommitError("only noop/focus_note may use receipt-free approval")
    # PowerShell may coerce nested ISO strings while round-tripping JSON.  Build
    # the receipt-free payload from the original pending object so its immutable
    # fields remain byte-for-byte semantic equivalents.
    normalized = dict(pending)
    normalized["status"] = "approved"
    normalized["approved_at"] = approved.get("approved_at")
    normalized["approved_by"] = "local_human"
    return normalized


def _fsync_file(path: pathlib.Path) -> None:
    # Windows FlushFileBuffers requires a handle opened for writing; a read-only
    # descriptor raises EBADF even though the file itself is valid.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _path_present(path: pathlib.Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _absolute_no_follow(path: pathlib.Path) -> pathlib.Path:
    """Make an absolute path without resolving symlinks or reparse points."""

    return pathlib.Path(path).expanduser().absolute()


def _safe_directory_metadata(path: pathlib.Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ApprovalCommitError(f"{label} cannot be inspected: {path}") from exc
    if path.is_symlink() or bool(int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT):
        raise ApprovalCommitError(f"{label} is a symlink or reparse point: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ApprovalCommitError(f"{label} is not a directory: {path}")
    return metadata


def _create_approval_recovery_dir(
    commands_root: pathlib.Path,
) -> tuple[pathlib.Path, tuple[int, int]]:
    """Create only this transaction's recovery directory; never adopt an old one."""

    parent_before = _safe_directory_metadata(commands_root, label="commands directory")
    recovery_dir = commands_root / _APPROVAL_RECOVERY_DIR_NAME
    try:
        recovery_dir.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ApprovalCommitError("approval recovery directory cannot be inspected") from exc
    else:
        raise ApprovalCommitError("approval recovery directory already exists")
    try:
        recovery_dir.mkdir()
    except FileExistsError as exc:
        raise ApprovalCommitError("approval recovery directory appeared after preflight") from exc
    created = _safe_directory_metadata(recovery_dir, label="approval recovery directory")
    parent_after = _safe_directory_metadata(commands_root, label="commands directory")
    if (int(parent_before.st_dev), int(parent_before.st_ino)) != (
        int(parent_after.st_dev),
        int(parent_after.st_ino),
    ):
        raise ApprovalCommitError("commands directory changed while creating approval recovery")
    return recovery_dir, (int(created.st_dev), int(created.st_ino))


def _remove_new_empty_recovery_dir(
    recovery_dir: pathlib.Path,
    *,
    identity: tuple[int, int] | None,
) -> None:
    # Conservative post-fail policy: never remove recovery directories.
    return


def _stage_snapshot(commands_root: pathlib.Path, name: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for stage in _QUEUE_STAGES:
        candidate = commands_root / stage / name
        if _path_present(candidate):
            result[stage] = str(candidate)
    return result


def _format_stage_snapshot(snapshot: Dict[str, str]) -> str:
    return ",".join(sorted(snapshot)) if snapshot else "none"


def _unique_recovery_path(
    recovery_dir: pathlib.Path,
    command_id: str,
    label: str,
) -> pathlib.Path:
    for nonce in range(100):
        candidate = recovery_dir / (
            f"{command_id}.{os.getpid()}.{time.time_ns()}.{nonce}.{label}"
        )
        if not _path_present(candidate):
            return candidate
    raise OSError("could not allocate a unique approval recovery evidence path")


def _write_ambiguity_evidence(
    recovery_dir: pathlib.Path,
    *,
    command_id: str,
    original_error: BaseException,
    recovery_error: str,
    stages: Dict[str, str],
    recovery: pathlib.Path | None,
    stage: pathlib.Path | None,
    retired_claim: pathlib.Path | None,
    claimed: bool,
    published: bool,
) -> pathlib.Path | None:
    record = {
        "schema": "oni.approval-recovery-evidence.v1",
        "command_id": command_id,
        "classification": "outcome_unknown",
        "claimed": claimed,
        "published": published,
        "original_error": f"{type(original_error).__name__}: {original_error}",
        "recovery_error": recovery_error,
        "queue_stages": dict(sorted(stages.items())),
        "pending_recovery_file": (
            str(recovery) if recovery is not None and _path_present(recovery) else None
        ),
        "approved_stage_file": (
            str(stage) if stage is not None and _path_present(stage) else None
        ),
        "reclaimed_file": (
            str(retired_claim)
            if retired_claim is not None and _path_present(retired_claim)
            else None
        ),
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    try:
        path = _unique_recovery_path(recovery_dir, command_id, "ambiguity.json")
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while recording approval ambiguity")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path
    except (OSError, UnicodeError, TypeError, ValueError):
        return None


def _recover_owned_failure(
    *,
    commands_root: pathlib.Path,
    source: pathlib.Path,
    target: pathlib.Path,
    claim: pathlib.Path,
    recovery: pathlib.Path | None,
    recovery_dir: pathlib.Path,
    expected_command_sha256: str,
    claimed: bool,
    published: bool,
) -> Dict[str, Any]:
    """Restore pending only while this process can prove it owns the sole copy."""

    retired_claim: pathlib.Path | None = None

    def result(safe: bool, reason: str) -> Dict[str, Any]:
        return {
            "safe": safe,
            "reason": reason,
            "stages": _stage_snapshot(commands_root, source.name),
            "retired_claim": retired_claim,
        }

    try:
        snapshot = _stage_snapshot(commands_root, source.name)
        if "pending" in snapshot:
            if set(snapshot) != {"pending"}:
                return result(False, "pending coexists with a later queue stage")
            return result(True, "original pending command remained authoritative")

        if recovery is None or not _path_present(recovery):
            return result(False, "the exact original pending recovery payload is unavailable")
        if _sha256(recovery) != expected_command_sha256.upper():
            return result(False, "the pending recovery payload hash changed")

        # Once the approved rename completed, the executor may race this process.
        # Only winning an atomic approved -> approving reclaim proves that dispatch
        # did not cross the durable execution-claim boundary.
        published_copy_present = published or "approved" in snapshot
        if published_copy_present:
            later = sorted(set(snapshot) & set(_EXECUTOR_OR_TERMINAL_STAGES))
            if later:
                return result(
                    False,
                    "the command already entered an executor or terminal stage: " + ",".join(later),
                )
            if "approving" in snapshot or "approved" not in snapshot:
                return result(False, "the published command is missing or has an ambiguous approving copy")
            try:
                os.replace(target, claim)
            except OSError as exc:
                return result(False, f"atomic approved reclaim failed: {type(exc).__name__}: {exc}")
            claimed = True
            published = False
            snapshot = _stage_snapshot(commands_root, source.name)
            later = sorted(set(snapshot) & ({"approved"} | set(_EXECUTOR_OR_TERMINAL_STAGES)))
            if later or set(snapshot) != {"approving"}:
                return result(
                    False,
                    "queue state changed after atomic approved reclaim: "
                    + _format_stage_snapshot(snapshot),
                )
        elif not claimed or set(snapshot) != {"approving"}:
            return result(
                False,
                "the claimed pending command cannot be uniquely located: "
                + _format_stage_snapshot(snapshot),
            )

        retired_claim = _unique_recovery_path(
            recovery_dir,
            source.stem,
            "reclaimed.tmp",
        )
        os.replace(claim, retired_claim)
        if _stage_snapshot(commands_root, source.name):
            return result(False, "another queue copy appeared while retiring the approval claim")

        os.replace(recovery, source)
        final_snapshot = _stage_snapshot(commands_root, source.name)
        if set(final_snapshot) != {"pending"}:
            # Never leave a replayable pending copy next to a later-stage copy.
            if "pending" in final_snapshot and _path_present(source):
                preserved_pending = _unique_recovery_path(
                    recovery_dir,
                    source.stem,
                    "restored-pending.tmp",
                )
                try:
                    os.replace(source, preserved_pending)
                except OSError:
                    pass
            return result(
                False,
                "pending restoration raced another queue transition: "
                + _format_stage_snapshot(final_snapshot),
            )

        try:
            retired_claim.unlink(missing_ok=True)
        except OSError:
            # The executor-visible ambiguity is closed; leftover reclaimed bytes
            # are harmless evidence in the non-executing recovery directory.
            pass
        return result(True, "pending command was restored after exclusive reclaim")
    except Exception as exc:
        return result(False, f"approval recovery failed: {type(exc).__name__}: {exc}")


def _unlink_owned_lock(
    lock: pathlib.Path,
    *,
    identity: tuple[int, int] | None,
    token: bytes | None,
) -> None:
    if identity is None or token is None:
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(str(lock), os.O_RDONLY)
        metadata = os.fstat(descriptor)
        if (int(metadata.st_dev), int(metadata.st_ino)) != identity:
            return
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            payload.extend(chunk)
        if bytes(payload) != token:
            return
    except OSError:
        return
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                # A close failure cannot authorize deletion through a path that
                # may no longer name the owned lock.  Conservatively retain it.
                return
    try:
        metadata = lock.lstat()
        if (int(metadata.st_dev), int(metadata.st_ino)) == identity:
            lock.unlink()
    except OSError:
        pass


def _commit_locked(
    args: argparse.Namespace,
    *,
    bridge_root: pathlib.Path,
    commands_root: pathlib.Path,
    source: pathlib.Path,
    target: pathlib.Path,
) -> pathlib.Path:
    payload_path = _absolute_no_follow(args.approved_payload)
    policy_path = _absolute_no_follow(args.policy)
    frame_path = _absolute_no_follow(args.frame) if args.frame is not None else None
    if source.parent.name != "pending" or target.parent.name != "approved":
        raise ApprovalCommitError("approval paths are outside pending/approved queue states")
    expected_source = command_custody.queue_stage_path(bridge_root, "pending", source.name)
    expected_target = command_custody.queue_stage_path(bridge_root, "approved", source.name)
    if not _same_path(source, expected_source) or not _same_path(target, expected_target):
        raise ApprovalCommitError("approval source and target do not share one command queue")
    command_id = source.stem
    if not _same_path(policy_path, bridge_root / "action-policy.json"):
        raise ApprovalCommitError("approval policy path is not the current Bridge action policy")
    if not _path_present(source) or not _path_present(payload_path) or not _path_present(policy_path):
        raise ApprovalCommitError("approval input file is missing")
    if args.expected_command_sha256 is None or args.expected_policy_sha256 is None:
        raise ApprovalCommitError("approval commit requires command and policy stability hashes")

    _require_hash(source, args.expected_command_sha256, "pending command")
    _require_hash(policy_path, args.expected_policy_sha256, "action policy")
    if frame_path is not None:
        if not frame_path.is_file():
            raise ApprovalCommitError("decision frame is missing")
        _require_hash(frame_path, args.expected_frame_sha256, "decision frame")

    pending = _load_object(source)
    approved = _load_object(payload_path)
    if pending.get("id") != command_id:
        raise ApprovalCommitError("pending command id does not match its file name")
    mutating_v3 = (
        pending.get("schema") == "oni.command.v3"
        and pending.get("action") not in {"noop", "focus_note"}
    )
    if mutating_v3:
        if args.expected_frame_sha256 is None:
            raise ApprovalCommitError("mutating approval commit requires an archive stability hash")
        try:
            expected_archive_path = decision_frame_archive_path(
                bridge_root,
                pending.get("source_frame_id"),
            )
        except DecisionFrameArchiveError as exc:
            raise ApprovalCommitError(str(exc)) from exc
        if frame_path is None or not _same_path(frame_path, expected_archive_path):
            raise ApprovalCommitError(
                "approval must use the exact archive named by command source_frame_id"
            )
    elif frame_path is not None:
        raise ApprovalCommitError("receipt-free approval must not select a decision frame")
    approved = _validate_payloads(pending, approved, policy_path, frame_path)

    approving_dir = command_custody.queue_stage_path(bridge_root, "approving", source.name).parent
    claim = approving_dir / source.name
    lock = commands_root / f".approval-{command_id}.lock"
    lock_fd: int | None = None
    lock_owned = False
    lock_identity: tuple[int, int] | None = None
    lock_token: bytes | None = None
    stage: pathlib.Path | None = None
    recovery: pathlib.Path | None = None
    recovery_dir = commands_root / _APPROVAL_RECOVERY_DIR_NAME
    recovery_dir_created = False
    recovery_dir_identity: tuple[int, int] | None = None
    preserve_recovery_dir = False
    claimed = False
    published = False
    preserve_stage = False
    preserve_recovery = False
    try:
        try:
            lock_fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ApprovalCommitError("another approval commit already owns this command") from exc
        lock_owned = True
        lock_metadata = os.fstat(lock_fd)
        lock_identity = (int(lock_metadata.st_dev), int(lock_metadata.st_ino))
        lock_token = f"{os.getpid()}:{time.time_ns()}".encode("ascii")
        os.write(lock_fd, lock_token)
        os.fsync(lock_fd)

        if _path_present(target) or _path_present(claim):
            raise ApprovalCommitError("command already exists in approved/approving")
        for directory_name in ("executing", "confirming", "done", "failed", "rejected", "quarantine"):
            if _path_present(commands_root / directory_name / source.name):
                raise ApprovalCommitError("command id already entered a later queue state")
        _require_hash(source, args.expected_command_sha256, "pending command")
        _require_hash(policy_path, args.expected_policy_sha256, "action policy")
        if frame_path is not None:
            _require_hash(frame_path, args.expected_frame_sha256, "decision frame")

        original_pending_bytes = source.read_bytes()
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(approving_dir), suffix=".tmp") as handle:
            recovery = pathlib.Path(handle.name)
            handle.write(original_pending_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(approving_dir), suffix=".tmp") as handle:
            stage = pathlib.Path(handle.name)
            data = (
                json.dumps(
                    approved,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        # Exactly one JSON queue file is visible after each rename: pending ->
        # approving(old payload) -> approving(approved payload) -> approved.
        os.replace(source, claim)
        claimed = True
        _require_hash(claim, args.expected_command_sha256, "claimed pending command")
        _require_hash(policy_path, args.expected_policy_sha256, "action policy")
        if frame_path is not None:
            _require_hash(frame_path, args.expected_frame_sha256, "decision frame")
        claimed_pending = _load_object(claim)
        staged_approved = _load_object(stage)
        _validate_payloads(claimed_pending, staged_approved, policy_path, frame_path)

        os.replace(stage, claim)
        stage = None
        _require_hash(policy_path, args.expected_policy_sha256, "action policy")
        if frame_path is not None:
            _require_hash(frame_path, args.expected_frame_sha256, "decision frame")
        committed_approved = _load_object(claim)
        _validate_payloads(pending, committed_approved, policy_path, frame_path)

        os.replace(claim, target)
        claimed = False
        published = True
        try:
            _fsync_file(target)
        except OSError as exc:
            raise ApprovalCommitError(
                "approved command changed before durable publication"
            ) from exc
        # A deterministic post-publication check detects a write racing the
        # final atomic rename.  On detection the command is immediately removed
        # from the executor-visible queue and restored to pending below.
        _require_hash(policy_path, args.expected_policy_sha256, "action policy")
        if frame_path is not None:
            _require_hash(frame_path, args.expected_frame_sha256, "decision frame")
        committed_target = _load_object(target)
        _validate_payloads(pending, committed_target, policy_path, frame_path)
        return target
    except Exception as exc:
        if not lock_owned:
            raise
        recovery_dir_trusted = False
        if claimed or published:
            try:
                recovery_dir, recovery_dir_identity = _create_approval_recovery_dir(commands_root)
                recovery_dir_created = True
            except Exception:
                preserve_recovery_dir = True

            recovery_dir_trusted = _recovery_dir_is_owned(
                commands_root=commands_root,
                recovery_dir=recovery_dir,
                identity=recovery_dir_identity,
            )

        if claimed or published:
            if recovery_dir_trusted:
                recovery_result = _recover_owned_failure(
                    commands_root=commands_root,
                    source=source,
                    target=target,
                    claim=claim,
                    recovery=recovery,
                    recovery_dir=recovery_dir,
                    expected_command_sha256=str(args.expected_command_sha256),
                    claimed=claimed,
                    published=published,
                )
            else:
                preserve_recovery = recovery is not None
                preserve_stage = stage is not None
                preserve_recovery_dir = True
                recovery_result = {
                    "safe": False,
                    "reason": "approval recovery directory ownership was not established",
                    "stages": _stage_snapshot(commands_root, source.name),
                    "retired_claim": None,
                }
        else:
            try:
                before_claim_stages = _stage_snapshot(commands_root, source.name)
                pending_remained_authoritative = (
                    set(before_claim_stages) == {"pending"}
                    and _sha256(source) == str(args.expected_command_sha256).upper()
                )
            except (OSError, ApprovalCommitError):
                before_claim_stages = {}
                pending_remained_authoritative = False
            recovery_result = {
                "safe": pending_remained_authoritative,
                "reason": "approval failed before claiming pending or approved state",
                "stages": before_claim_stages,
                "retired_claim": None,
            }
        stages = recovery_result["stages"]
        if recovery_result["safe"] and set(stages) == {"pending"}:
            raise ApprovalCommitError(
                "approval commit failed but pending ownership was safely restored; "
                f"queue_stages={_format_stage_snapshot(stages)}; "
                f"cause={type(exc).__name__}: {exc}"
            ) from exc

        preserve_recovery = recovery is not None
        preserve_stage = stage is not None
        evidence_path = None
        evidence_dir_trusted = recovery_dir_created and _recovery_dir_is_owned(
            commands_root=commands_root, recovery_dir=recovery_dir, identity=recovery_dir_identity
        )
        if evidence_dir_trusted:
            evidence_path = _write_ambiguity_evidence(
                recovery_dir,
                command_id=command_id,
                original_error=exc,
                recovery_error=str(recovery_result["reason"]),
                stages=stages,
                recovery=recovery,
                stage=stage,
                retired_claim=recovery_result.get("retired_claim"),
                claimed=claimed,
                published=published,
            )
        else:
            preserve_recovery_dir = True

        evidence_label = str(evidence_path) if evidence_path is not None else "unavailable"
        raise ApprovalOutcomeUnknown(
            "outcome_unknown: approval publication could not be safely reclaimed; "
            f"queue_stages={_format_stage_snapshot(stages)}; "
            f"recovery_evidence={evidence_label}; "
            f"recovery_error={recovery_result['reason']}; "
            "do not restore or replay the pending command"
        ) from exc
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                # Do not let lock-handle cleanup replace a committed success or
                # the ownership-recovery result already chosen above.
                pass
        if lock_owned:
            _unlink_owned_lock(lock, identity=lock_identity, token=lock_token)
        if stage is not None and not preserve_stage:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
        if recovery is not None and not preserve_recovery:
            try:
                recovery.unlink(missing_ok=True)
            except OSError:
                pass
        if recovery_dir_created and not preserve_recovery_dir:
            _remove_new_empty_recovery_dir(
                recovery_dir,
                identity=recovery_dir_identity,
            )


def commit(args: argparse.Namespace) -> pathlib.Path:
    source = _absolute_no_follow(args.command)
    target = _absolute_no_follow(args.target)
    if source.parent.name != "pending" or target.parent.name != "approved":
        raise ApprovalCommitError("approval paths are outside pending/approved queue states")
    commands_candidate = source.parent.parent
    if commands_candidate.name != "commands":
        raise ApprovalCommitError("approval queue path is not under a commands directory")
    lexical_source = commands_candidate / "pending" / source.name
    lexical_target = commands_candidate / "approved" / source.name
    if not _same_path(source, lexical_source) or not _same_path(target, lexical_target):
        raise ApprovalCommitError("approval source and target are not one exact pending/approved queue entry")
    bridge_root = commands_candidate.parent
    try:
        commands_root = command_custody.commands_root_for(bridge_root)
    except command_custody.CommandCustodyError as exc:
        raise ApprovalCommitError(f"approval queue path is unsafe: {exc}") from exc
    if not _same_path(commands_root, commands_candidate):
        raise ApprovalCommitError("approval paths are outside the current Bridge command queue")
    try:
        with command_custody.lifecycle_custody(bridge_root):
            expected_source = command_custody.queue_stage_path(bridge_root, "pending", source.name)
            expected_target = command_custody.queue_stage_path(bridge_root, "approved", source.name)
            if not _same_path(source, expected_source) or not _same_path(target, expected_target):
                raise ApprovalCommitError("approval source and target are not exact canonical queue paths")
            private_lock = commands_root / f".approval-{source.stem}.lock"
            if _path_present(private_lock):
                raise ApprovalCommitError("another approval commit already owns this command")
            try:
                command_custody.recover_lifecycle_transactions(bridge_root)
            except command_custody.CommandCustodyError as exc:
                raise ApprovalCommitError(
                    f"shared lifecycle preflight rejected approval: {exc}"
                ) from exc
            return _commit_locked(
                args,
                bridge_root=bridge_root,
                commands_root=commands_root,
                source=source,
                target=target,
            )
    except command_custody.CommandCustodyError as exc:
        raise ApprovalCommitError(f"shared lifecycle custody rejected approval: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", type=pathlib.Path, required=True)
    parser.add_argument("--target", type=pathlib.Path, required=True)
    parser.add_argument("--approved-payload", type=pathlib.Path, required=True)
    parser.add_argument("--policy", type=pathlib.Path, required=True)
    parser.add_argument("--frame", type=pathlib.Path)
    parser.add_argument("--expected-command-sha256")
    parser.add_argument("--expected-policy-sha256")
    parser.add_argument("--expected-frame-sha256")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        target = commit(args)
    except (ApprovalCommitError, OSError, ValueError, TypeError) as exc:
        print(f"approval commit rejected: {exc}", file=sys.stderr)
        return 1
    print(str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
