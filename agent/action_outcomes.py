from __future__ import annotations

import copy
import datetime as dt
import errno
import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
import time
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from agent.command_custody import (
    CommandCustodyError,
    lifecycle_custody,
    read_bytes_share_delete,
    read_stable_bytes,
)
from agent.command_queue import COMMAND_SCHEMA, ensure_command_dirs, write_json_atomic
from agent.action_policy import (
    ACTION_POLICY_SCHEMA,
    AUTONOMOUS_ACTION_POLICY_SCHEMA,
    action_policy_is_valid,
    autonomous_identifier_is_valid,
)
from agent.canonical_hash import canonical_hash as _contract_canonical_hash, loads_strict
from agent.decision import DecisionFrameError, validate_decision_frame
from agent.decision_frame_archive import (
    DecisionFrameArchiveError,
    load_archived_decision_frame,
)
from agent.field_contracts import NON_MUTATING_ACTIONS, condition_contract_blockers
from agent.state_quality import (
    canonical_state_hash,
    parse_positive_sequence,
    parse_snapshot_timestamp,
    parse_timestamp,
    safe_get,
    snapshot_trust_blockers,
)


ACTION_OUTCOMES_SCHEMA = "oni.action-outcomes.v1"
ACTION_OUTCOME_SCHEMA = "oni.action-outcome.v1"
ACTION_OUTCOMES_COMMIT_SCHEMA = "oni.action-outcomes-commit.v1"
CONFIRMED_COMMAND_APPROVAL_BUNDLE_SCHEMA = "oni.confirmed-command-approval-bundle.v1"
APPROVAL_RECEIPT_SCHEMA = "oni.approval-receipt.v1"
AUTONOMOUS_APPROVAL_RECEIPT_SCHEMA = "oni.approval-receipt.v2"
AUTONOMOUS_COMMAND_SCHEMA = "oni.command.v4"
CONFIRMATION_EXECUTED = "executed_verified"
CONFIRMATION_WAITING = "awaiting_trusted_snapshot"
CONFIRMATION_CONFIRMED = "snapshot_confirmed"
CONFIRMATION_REJECTED = "rejected"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_RETRYABLE_MOVE_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
_RETRYABLE_MOVE_WINERRORS = {5, 32, 33}
_MOVE_ATTEMPTS = 8
_MOVE_INITIAL_DELAY_SECONDS = 0.025
_MOVE_MAX_DELAY_SECONDS = 0.2
_COMMAND_INTENT_FIELDS: Tuple[str, ...] = (
    "schema",
    "id",
    "mode",
    "action",
    "args",
    "session_id",
    "save_id_hash",
    "world_id",
    "source_incident_id",
    "source_snapshot_id",
    "state_hash",
    "source_frame_id",
    "created_at",
    "expires_at",
    "risk",
    "reason",
    "evidence",
    "preconditions",
    "expected_effects",
    "requires_approval",
    "source",
)
_APPROVAL_BUNDLE_ACTIONS = frozenset({"set_duplicant_chore_priority"})
_APPROVAL_BUNDLE_KEYS = frozenset(
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


class ActionOutcomeError(RuntimeError):
    pass


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical_hash(value: Any) -> str:
    return _contract_canonical_hash(value)


def command_intent_projection(command: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep the legacy intent byte-for-byte stable; cover V4 authority too."""

    fields = _COMMAND_INTENT_FIELDS + (
        ("run_id", "controller_id")
        if command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
        else ()
    )
    return {field: copy.deepcopy(command.get(field)) for field in fields}


def command_intent_hash(command: Mapping[str, Any]) -> str:
    """Hash the immutable command intent independently of executor result fields."""

    return _canonical_hash(command_intent_projection(command))


def confirmed_command_approval_bundle_path(
    bridge_dir: pathlib.Path,
    command_id: Any,
) -> pathlib.Path:
    """Return a traversal-safe immutable evidence path for one command id."""

    if not isinstance(command_id, str) or not command_id:
        raise ActionOutcomeError("confirmed command approval bundle id is invalid")
    digest = hashlib.sha256(command_id.encode("utf-8", errors="strict")).hexdigest()
    return (
        pathlib.Path(bridge_dir).expanduser().resolve()
        / "confirmed-command-evidence"
        / digest
        / "approval-bundle.json"
    )


def _approval_bundle_blockers(bundle: Mapping[str, Any]) -> List[str]:
    blockers: List[str] = []
    if set(bundle) != _APPROVAL_BUNDLE_KEYS:
        blockers.append("approval_bundle_shape_invalid")
    if bundle.get("schema") != CONFIRMED_COMMAND_APPROVAL_BUNDLE_SCHEMA:
        blockers.append("approval_bundle_schema_invalid")
    command = bundle.get("final_command")
    if not isinstance(command, Mapping):
        blockers.append("approval_bundle_command_missing")
        return sorted(set(blockers))
    if bundle.get("command_id") != command.get("id"):
        blockers.append("approval_bundle_command_id_mismatch")
    if bundle.get("action") != command.get("action"):
        blockers.append("approval_bundle_action_mismatch")
    if command.get("action") not in _APPROVAL_BUNDLE_ACTIONS:
        blockers.append("approval_bundle_action_unsupported")
    if command.get("status") != "confirmed":
        blockers.append("approval_bundle_command_not_confirmed")
    try:
        intent_hash = command_intent_hash(command)
        final_hash = _canonical_hash(command)
    except (TypeError, ValueError, OverflowError):
        blockers.append("approval_bundle_command_hash_invalid")
        intent_hash = None
        final_hash = None
    if bundle.get("command_intent_hash") != intent_hash:
        blockers.append("approval_bundle_intent_hash_mismatch")
    if bundle.get("final_command_hash") != final_hash:
        blockers.append("approval_bundle_final_command_hash_mismatch")
    receipt_blockers = _approval_receipt_blockers(command)
    blockers.extend(f"approval_bundle_{item}" for item in receipt_blockers)
    receipt = command.get("approval_receipt") if isinstance(command.get("approval_receipt"), Mapping) else {}
    command_source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
    policy_hash = command_source.get("policy_hash")
    if (
        not isinstance(policy_hash, str)
        or _HASH_RE.fullmatch(policy_hash) is None
        or receipt.get("policy_hash") != policy_hash
        or bundle.get("source_policy_hash") != policy_hash
    ):
        blockers.append("approval_bundle_policy_binding_invalid")
    frozen_policy = bundle.get("source_policy")
    if not isinstance(frozen_policy, Mapping):
        blockers.append("approval_bundle_source_policy_missing")
    else:
        policy_document = frozen_policy.get("document")
        policy_file_hash = frozen_policy.get("file_sha256")
        autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
        if (
            not isinstance(policy_document, Mapping)
            or policy_document.get("schema") != (AUTONOMOUS_ACTION_POLICY_SCHEMA if autonomous else ACTION_POLICY_SCHEMA)
            or policy_document.get("actions_enabled") is not True
            or not isinstance(policy_document.get("allowed_actions"), list)
            or command.get("action") not in policy_document.get("allowed_actions", [])
            or not isinstance(policy_document.get("allowed_save_hashes"), list)
            or command.get("save_id_hash") not in policy_document.get("allowed_save_hashes", [])
            or _canonical_hash(policy_document) != policy_hash
            or frozen_policy.get("policy_hash") != policy_hash
        ):
            blockers.append("approval_bundle_source_policy_document_mismatch")
        if autonomous and isinstance(policy_document, Mapping):
            if (
                not action_policy_is_valid(policy_document)
                or any(command.get(key) != policy_document.get(key) for key in ("run_id", "controller_id", "session_id", "world_id"))
                or policy_document.get("allowed_save_hashes") != [command.get("save_id_hash")]
            ):
                blockers.append("approval_bundle_autonomous_policy_identity_mismatch")
        if not isinstance(policy_file_hash, str) or _HASH_RE.fullmatch(policy_file_hash) is None:
            blockers.append("approval_bundle_source_policy_file_hash_invalid")
    if bundle.get("approval_receipt_hash") != receipt.get("receipt_hash"):
        blockers.append("approval_bundle_receipt_hash_mismatch")

    source_frame = bundle.get("source_frame")
    if not isinstance(source_frame, Mapping):
        blockers.append("approval_bundle_source_frame_missing")
    else:
        frame = source_frame.get("document")
        frame_id = command.get("source_frame_id")
        if (
            not isinstance(frame_id, str)
            or _HASH_RE.fullmatch(frame_id) is None
            or source_frame.get("frame_id") != frame_id
        ):
            blockers.append("approval_bundle_source_frame_id_mismatch")
        archive_hash = source_frame.get("archive_file_sha256")
        if not isinstance(archive_hash, str) or _HASH_RE.fullmatch(archive_hash) is None:
            blockers.append("approval_bundle_source_frame_file_hash_invalid")
        if not isinstance(frame, Mapping):
            blockers.append("approval_bundle_source_frame_document_missing")
        else:
            try:
                validate_decision_frame(frame)
            except (DecisionFrameError, TypeError, ValueError, OverflowError):
                blockers.append("approval_bundle_source_frame_invalid")
            if frame.get("frame_id") != frame_id:
                blockers.append("approval_bundle_source_frame_payload_mismatch")
            frame_source = frame.get("source") if isinstance(frame.get("source"), Mapping) else {}
            expected_source = {
                "session_id": command.get("session_id"),
                "save_id_hash": command.get("save_id_hash"),
                "world_id": command.get("world_id"),
                "snapshot_id": command.get("source_snapshot_id"),
                "state_hash": command.get("state_hash"),
            }
            if any(frame_source.get(key) != value for key, value in expected_source.items()):
                blockers.append("approval_bundle_source_frame_identity_mismatch")
            if (
                frame_source.get("seq") != command_source.get("state_seq")
                or frame_source.get("ts") != command_source.get("state_ts")
            ):
                blockers.append("approval_bundle_source_frame_freshness_mismatch")
            trust = frame.get("trust") if isinstance(frame.get("trust"), Mapping) else {}
            if trust.get("status") != "trusted":
                blockers.append("approval_bundle_source_frame_untrusted")
            projections = (
                frame.get("projections") if isinstance(frame.get("projections"), Mapping) else {}
            )
            policy_projection = (
                projections.get("policy")
                if isinstance(projections.get("policy"), Mapping)
                else {}
            )
            if policy_projection.get("policy_hash") != policy_hash:
                blockers.append("approval_bundle_source_frame_policy_hash_mismatch")
    declared_bundle_hash = bundle.get("bundle_hash")
    if (
        not isinstance(declared_bundle_hash, str)
        or _HASH_RE.fullmatch(declared_bundle_hash) is None
        or declared_bundle_hash != _hash_document(bundle, "bundle_hash")
    ):
        blockers.append("approval_bundle_hash_mismatch")
    return sorted(set(blockers))


def validate_confirmed_command_approval_bundle(bundle: Mapping[str, Any]) -> None:
    blockers = _approval_bundle_blockers(bundle)
    if blockers:
        raise ActionOutcomeError(
            "confirmed command approval bundle is invalid: " + ",".join(blockers)
        )


def load_confirmed_command_approval_bundle(
    bridge_dir: pathlib.Path,
    command_id: Any,
) -> Tuple[Dict[str, Any], pathlib.Path]:
    path = confirmed_command_approval_bundle_path(bridge_dir, command_id)
    bundle = _read_json_object(path)
    if not isinstance(bundle, dict):
        raise ActionOutcomeError("confirmed command approval bundle is missing or unreadable")
    validate_confirmed_command_approval_bundle(bundle)
    if bundle.get("command_id") != command_id:
        raise ActionOutcomeError("confirmed command approval bundle path/id mismatch")
    return bundle, path


def _publish_json_create_only(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    """Publish a complete JSON document without an overwrite window."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8", errors="strict")
    temporary: Optional[pathlib.Path] = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent), suffix=".tmp") as handle:
            temporary = pathlib.Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def persist_confirmed_command_approval_bundle(
    bridge_dir: pathlib.Path,
    final_command: Mapping[str, Any],
) -> Tuple[Dict[str, Any], pathlib.Path]:
    """Freeze approval authority before a priority command may enter ``done``.

    The operation is idempotent.  An existing byte-independent canonical match
    is accepted; any conflicting payload fails closed and is never replaced.
    """

    command_id = final_command.get("id")
    if final_command.get("action") not in _APPROVAL_BUNDLE_ACTIONS:
        raise ActionOutcomeError("confirmed command approval bundle action is unsupported")
    # An existing bundle already froze the source frame and policy.  Re-entry
    # for a command that is already in ``done`` must accept it as it is: the
    # frame is no longer referenced by any nonterminal command, so archive
    # pruning may have removed it, and a stop disables the policy.  Rebuilding
    # from those inputs raised "could not be frozen before done" for commands
    # long since done and took the Bridge down (Phase 2 rounds 28 and 51,
    # roadmap B8).  The bundle must still be valid and freeze exactly this
    # final command; anything else stays a conflict.
    target = confirmed_command_approval_bundle_path(pathlib.Path(bridge_dir), command_id)
    if target.exists():
        existing, existing_path = load_confirmed_command_approval_bundle(bridge_dir, command_id)
        if (
            not _strict_json_equal(existing.get("final_command"), dict(final_command))
            or existing.get("final_command_hash") != _canonical_hash(final_command)
        ):
            raise ActionOutcomeError("existing confirmed command approval bundle conflicts")
        return existing, existing_path
    try:
        frame, frame_path, _archive_hash = load_archived_decision_frame(
            pathlib.Path(bridge_dir),
            final_command.get("source_frame_id"),
        )
    except DecisionFrameArchiveError as exc:
        raise ActionOutcomeError(
            "confirmed command source frame could not be frozen before done"
        ) from exc
    archive_file_hash = _file_sha256(frame_path)
    if archive_file_hash is None:
        raise ActionOutcomeError("confirmed command source frame hash is unavailable")
    receipt = (
        final_command.get("approval_receipt")
        if isinstance(final_command.get("approval_receipt"), Mapping)
        else {}
    )
    source = final_command.get("source") if isinstance(final_command.get("source"), Mapping) else {}
    policy_path = pathlib.Path(bridge_dir).expanduser().resolve() / "action-policy.json"
    policy = _read_json_object(policy_path)
    if not isinstance(policy, dict):
        raise ActionOutcomeError("confirmed command source policy is missing or unreadable")
    source_policy_hash = source.get("policy_hash")
    if (
        not isinstance(source_policy_hash, str)
        or _HASH_RE.fullmatch(source_policy_hash) is None
        or _canonical_hash(policy) != source_policy_hash
    ):
        raise ActionOutcomeError("confirmed command source policy changed before done")
    policy_file_hash = _file_sha256(policy_path)
    if policy_file_hash is None:
        raise ActionOutcomeError("confirmed command source policy hash is unavailable")
    bundle: Dict[str, Any] = {
        "schema": CONFIRMED_COMMAND_APPROVAL_BUNDLE_SCHEMA,
        "command_id": command_id,
        "action": final_command.get("action"),
        "command_intent_hash": command_intent_hash(final_command),
        "final_command_hash": _canonical_hash(final_command),
        "final_command": copy.deepcopy(dict(final_command)),
        "source_policy_hash": source_policy_hash,
        "source_policy": {
            "policy_hash": source_policy_hash,
            "file_sha256": policy_file_hash,
            "document": copy.deepcopy(policy),
        },
        "approval_receipt_hash": receipt.get("receipt_hash"),
        "source_frame": {
            "frame_id": final_command.get("source_frame_id"),
            "archive_file_sha256": archive_file_hash,
            "document": copy.deepcopy(frame),
        },
        "bundle_hash": "",
    }
    bundle["bundle_hash"] = _hash_document(bundle, "bundle_hash")
    validate_confirmed_command_approval_bundle(bundle)
    target = confirmed_command_approval_bundle_path(pathlib.Path(bridge_dir), command_id)
    _publish_json_create_only(target, bundle)
    existing, existing_path = load_confirmed_command_approval_bundle(bridge_dir, command_id)
    if not _strict_json_equal(existing, bundle):
        raise ActionOutcomeError("existing confirmed command approval bundle conflicts")
    return existing, existing_path


def _hash_document(document: Mapping[str, Any], hash_field: str) -> str:
    value = copy.deepcopy(dict(document))
    value.pop(hash_field, None)
    return _canonical_hash(value)


def _strict_json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_strict_json_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(_strict_json_equal(left[key], right[key]) for key in left)
        )
    return left == right


def _condition_matches(
    state: Mapping[str, Any],
    condition: Mapping[str, Any],
    *,
    action: str,
) -> bool:
    if condition_contract_blockers(state, condition, action=action):
        return False
    marker = object()
    actual = safe_get(dict(state), str(condition.get("path") or ""), marker)
    if actual is marker:
        return False
    expected = condition.get("value")
    operator = condition.get("op")
    if operator == "eq":
        return _strict_json_equal(actual, expected)
    if operator == "ne":
        return not _strict_json_equal(actual, expected)
    if operator == "contains":
        return isinstance(actual, list) and expected in actual
    if operator not in {"lt", "lte", "gt", "gte"}:
        return False
    if (
        isinstance(actual, bool)
        or isinstance(expected, bool)
        or not isinstance(actual, (int, float))
        or not isinstance(expected, (int, float))
    ):
        return False
    try:
        left = float(actual)
        right = float(expected)
    except (OverflowError, TypeError, ValueError):
        return False
    if not math.isfinite(left) or not math.isfinite(right):
        return False
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    return left >= right


def _conditions_match(
    state: Mapping[str, Any],
    conditions: Sequence[Any],
    *,
    action: str,
) -> bool:
    return all(
        isinstance(condition, Mapping)
        and _condition_matches(state, condition, action=action)
        for condition in conditions
    )


def _aware_timestamp(value: Any) -> Optional[dt.datetime]:
    parsed = parse_timestamp(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _safe_session_name(value: Any) -> str:
    text = str(value or "waiting")
    return (_SESSION_SAFE_RE.sub("_", text)[:120] or "waiting")


def _read_json_object(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        # Never deny the executor's rename of a command file (review L10).
        value = loads_strict(read_bytes_share_delete(path).decode("utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _file_sha256(path: pathlib.Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def _command_locations(
    bridge_dir: pathlib.Path,
) -> Dict[str, List[Tuple[str, pathlib.Path, Dict[str, Any]]]]:
    grouped: Dict[str, List[Tuple[str, pathlib.Path, Dict[str, Any]]]] = {}
    for directory_name, directory in ensure_command_dirs(bridge_dir).items():
        for path in sorted(directory.glob("*.json")):
            command = _read_json_object(path)
            if not isinstance(command, dict):
                continue
            command_id = command.get("id")
            if not isinstance(command_id, str) or not command_id:
                continue
            grouped.setdefault(command_id, []).append((directory_name, path, command))
    return grouped


def _archive_candidate_lines(path: pathlib.Path, needles: Iterable[bytes]) -> Iterator[str]:
    """Yield decoded session-archive lines that contain any needle.

    The Bridge writes every archive line with ``json.dumps`` (see
    ``archive_session_records``), so a string value inside a line always
    appears as its JSON token and a line without any wanted token cannot
    match; only candidate lines are decoded and parsed.  Parsing every
    multi-megabyte snapshot line on every generation made each scan grow with
    the whole session (code review 2026-09-26, L7).
    """

    patterns = tuple(needles)
    first = True
    with path.open("rb") as handle:
        for raw in handle:
            leading = first
            first = False
            if not any(pattern in raw for pattern in patterns):
                continue
            yield raw.decode("utf-8-sig" if leading else "utf-8")


def _json_token(value: str) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _source_snapshots(
    bridge_dir: pathlib.Path,
    session_id: str,
    wanted_snapshot_ids: Iterable[str],
) -> Dict[str, List[Dict[str, Any]]]:
    wanted = set(wanted_snapshot_ids)
    found: Dict[str, List[Dict[str, Any]]] = {snapshot_id: [] for snapshot_id in wanted}
    if not wanted:
        return found
    path = bridge_dir / "sessions" / _safe_session_name(session_id) / "state.jsonl"
    try:
        for line in _archive_candidate_lines(path, [_json_token(item) for item in sorted(wanted)]):
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(value, dict):
                continue
            snapshot_id = value.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id in wanted:
                found[snapshot_id].append(value)
    except (FileNotFoundError, OSError, UnicodeError):
        return found
    return found


def _command_acks(
    bridge_dir: pathlib.Path,
    session_id: str,
    wanted_command_ids: Iterable[str],
) -> Dict[str, List[Dict[str, Any]]]:
    wanted = set(wanted_command_ids)
    found: Dict[str, List[Dict[str, Any]]] = {command_id: [] for command_id in wanted}
    if not wanted:
        return found
    path = bridge_dir / "sessions" / _safe_session_name(session_id) / "state.jsonl"
    try:
        for line in _archive_candidate_lines(path, [_json_token(item) for item in sorted(wanted)]):
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("event") == "command_ack"
                and isinstance(value.get("id"), str)
                and value.get("id") in wanted
            ):
                found[str(value["id"])].append(value)
    except (FileNotFoundError, OSError, UnicodeError):
        return found
    return found


def _identity_blockers(
    state: Mapping[str, Any],
    command: Mapping[str, Any],
    prefix: str,
) -> List[str]:
    blockers: List[str] = []
    for field in ("session_id", "save_id_hash", "world_id"):
        if state.get(field) != command.get(field):
            blockers.append(f"{prefix}_{field}_mismatch")
    if command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA and (prefix in {"source", "witness"} or prefix.endswith("_ack")):
        if state.get("validation_run_id") != command.get("run_id"):
            blockers.append(f"{prefix}_validation_run_id_mismatch")
        if state.get("startup_binding_status") != "verified":
            blockers.append(f"{prefix}_startup_binding_unverified")
    return blockers


def _approval_receipt_blockers(command: Mapping[str, Any]) -> List[str]:
    receipt = command.get("approval_receipt")
    if not isinstance(receipt, Mapping):
        return ["approval_receipt_missing"]
    required = {
        "schema",
        "command_id",
        "command_intent_hash",
        "source_frame_id",
        "source",
        "policy_hash",
        "approved_by",
        "approved_at",
        "receipt_hash",
    }
    autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
    if autonomous:
        required |= {"run_id", "controller_id"}
    blockers: List[str] = []
    if set(receipt) != required:
        blockers.append("approval_receipt_shape_invalid")
    if receipt.get("schema") != (AUTONOMOUS_APPROVAL_RECEIPT_SCHEMA if autonomous else APPROVAL_RECEIPT_SCHEMA):
        blockers.append("approval_receipt_schema_invalid")
    if command.get("schema") not in (COMMAND_SCHEMA, AUTONOMOUS_COMMAND_SCHEMA):
        blockers.append("approval_receipt_command_schema_invalid")
    if autonomous:
        for key in ("run_id", "controller_id"):
            if not autonomous_identifier_is_valid(command.get(key)) or receipt.get(key) != command.get(key):
                blockers.append(f"approval_receipt_{key}_mismatch")
        if command.get("mode") not in ("suggested", "model_selected"):
            blockers.append("approval_receipt_autonomous_mode_invalid")
        if type(command.get("world_id")) is not int or not 0 <= command["world_id"] <= 2147483647:
            blockers.append("approval_receipt_autonomous_world_invalid")
    if receipt.get("command_id") != command.get("id"):
        blockers.append("approval_receipt_command_id_mismatch")
    expected_intent_hash = command_intent_hash(command)
    if receipt.get("command_intent_hash") != expected_intent_hash:
        blockers.append("approval_receipt_intent_hash_mismatch")
    source_frame_id = command.get("source_frame_id")
    if (
        not isinstance(source_frame_id, str)
        or _HASH_RE.fullmatch(source_frame_id) is None
        or receipt.get("source_frame_id") != source_frame_id
    ):
        blockers.append("approval_receipt_frame_mismatch")
    source = receipt.get("source")
    expected_source = {
        "session_id": command.get("session_id"),
        "save_id_hash": command.get("save_id_hash"),
        "world_id": command.get("world_id"),
        "snapshot_id": command.get("source_snapshot_id"),
        "state_hash": command.get("state_hash"),
    }
    if not isinstance(source, Mapping) or not _strict_json_equal(dict(source), expected_source):
        blockers.append("approval_receipt_source_mismatch")
    authority = "autonomous_controller" if autonomous else "local_human"
    if receipt.get("approved_by") != authority or command.get("approved_by") != authority:
        blockers.append("approval_receipt_approver_mismatch")
    if receipt.get("approved_at") != command.get("approved_at"):
        blockers.append("approval_receipt_timestamp_mismatch")
    policy_hash = receipt.get("policy_hash")
    if not isinstance(policy_hash, str) or _HASH_RE.fullmatch(policy_hash) is None:
        blockers.append("approval_receipt_policy_hash_invalid")
    command_source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
    if command_source.get("policy_hash") != policy_hash:
        blockers.append("approval_receipt_source_policy_hash_mismatch")
    receipt_hash = receipt.get("receipt_hash")
    if (
        not isinstance(receipt_hash, str)
        or _HASH_RE.fullmatch(receipt_hash) is None
        or receipt_hash != _hash_document(receipt, "receipt_hash")
    ):
        blockers.append("approval_receipt_hash_mismatch")
    return sorted(set(blockers))


def _condition_blockers(
    state: Mapping[str, Any],
    conditions: Sequence[Any],
    *,
    action: str,
    prefix: str,
) -> List[str]:
    blockers: List[str] = []
    for index, condition in enumerate(conditions):
        if not isinstance(condition, Mapping):
            blockers.append(f"{prefix}_{index}_invalid")
            continue
        contract_errors = condition_contract_blockers(state, condition, action=action)
        blockers.extend(f"{prefix}_{index}_{error}" for error in contract_errors)
        if not contract_errors and not _condition_matches(state, condition, action=action):
            blockers.append(f"{prefix}_{index}_mismatch")
    return blockers


def _base_outcome(
    command: Mapping[str, Any],
    directory_name: str,
) -> Dict[str, Any]:
    return {
        "schema": ACTION_OUTCOME_SCHEMA,
        "command_id": command.get("id"),
        "action": command.get("action"),
        "command_intent_hash": command_intent_hash(command),
        "command_location": directory_name,
        "executor_status": command.get("status"),
        "confirmation_status": CONFIRMATION_EXECUTED,
        "classification": "behavior_fail",
        "blockers": [],
        "source": {
            "snapshot_id": command.get("source_snapshot_id"),
            "seq": None,
            "state_hash": command.get("state_hash"),
        },
        "direct": {
            "completed_at": command.get("completed_at"),
            "before_state_hash": None,
            "after_state_hash": None,
        },
        "witness": {
            "snapshot_id": None,
            "seq": None,
            "ts": None,
            "state_hash": None,
        },
        "attestation_hash": None,
    }


def _stored_confirmation(
    command: Mapping[str, Any],
    *,
    directory_name: str,
    location_count: int,
) -> Optional[Dict[str, Any]]:
    """Return a done command's intact stored attestation, if it has one."""

    stored_confirmation = command.get("confirmation")
    if not (
        directory_name == "done"
        and location_count == 1
        and command.get("status") == "confirmed"
        and isinstance(stored_confirmation, Mapping)
    ):
        return None
    stored = copy.deepcopy(dict(stored_confirmation))
    declared_hash = stored.get("attestation_hash")
    if (
        stored.get("schema") == ACTION_OUTCOME_SCHEMA
        and stored.get("command_id") == command.get("id")
        and stored.get("command_intent_hash") == command_intent_hash(command)
        and stored.get("confirmation_status") == CONFIRMATION_CONFIRMED
        and isinstance(declared_hash, str)
        and _HASH_RE.fullmatch(declared_hash) is not None
        and declared_hash == _hash_document(stored, "attestation_hash")
        and (
            command.get("action") in {"noop", "focus_note"}
            or not _approval_receipt_blockers(command)
        )
    ):
        return stored
    return None


def _attest_done_command(
    command: Mapping[str, Any],
    *,
    directory_name: str,
    location_count: int,
    source_records: Sequence[Dict[str, Any]],
    ack_records: Sequence[Dict[str, Any]],
    witness: Optional[Mapping[str, Any]],
    health: Mapping[str, Any],
    replay_mode: bool,
) -> Dict[str, Any]:
    stored = _stored_confirmation(
        command,
        directory_name=directory_name,
        location_count=location_count,
    )
    if stored is not None:
        return stored

    outcome = _base_outcome(command, directory_name)
    permanent: List[str] = []
    waiting: List[str] = []

    if location_count != 1:
        permanent.append("command_location_ambiguous")
    if directory_name not in {"confirming", "done"}:
        permanent.append("command_not_in_confirmation_pipeline")
    autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
    if command.get("schema") not in (COMMAND_SCHEMA, AUTONOMOUS_COMMAND_SCHEMA):
        permanent.append("command_schema_invalid")
    expected_executor_status = (
        "executed_pending_snapshot" if directory_name == "confirming" else "confirmed"
    )
    if command.get("status") != expected_executor_status:
        permanent.append("executor_status_invalid")
    if command.get("requires_approval") is not True:
        permanent.append("approval_requirement_missing")
    if command.get("approved_by") != ("autonomous_controller" if autonomous else "local_human"):
        permanent.append("autonomous_controller_approval_missing" if autonomous else "local_human_approval_missing")
    if not isinstance(command.get("id"), str) or not command.get("id"):
        permanent.append("command_id_invalid")
    action = command.get("action")
    if not isinstance(action, str) or not action:
        permanent.append("action_invalid")
        action = "unknown"
    if action not in {"noop", "focus_note"}:
        permanent.extend(_approval_receipt_blockers(command))
    if not isinstance(command.get("args"), dict):
        permanent.append("args_invalid")
    preconditions = command.get("preconditions")
    effects = command.get("expected_effects")
    if not isinstance(preconditions, list):
        permanent.append("preconditions_invalid")
        preconditions = []
    if not isinstance(effects, list):
        permanent.append("expected_effects_invalid")
        effects = []
    if action not in {"noop", "focus_note"} and (not preconditions or not effects):
        permanent.append("mutating_conditions_missing")

    attempt_id = command.get("attempt_id")
    if (
        not isinstance(attempt_id, str)
        or not attempt_id.startswith("attempt-")
        or len(attempt_id) != 40
    ):
        permanent.append("attempt_id_invalid")
    ack_sequences: Dict[str, Optional[int]] = {
        "accepted": parse_positive_sequence(command.get("accepted_ack_seq")),
        "executed": parse_positive_sequence(command.get("executed_ack_seq")),
        "local_effect_observed": parse_positive_sequence(command.get("local_effect_ack_seq")),
    }
    if any(value is None for value in ack_sequences.values()):
        permanent.append("ack_sequence_missing")
    elif not (
        int(ack_sequences["accepted"] or 0)
        < int(ack_sequences["executed"] or 0)
        < int(ack_sequences["local_effect_observed"] or 0)
    ):
        permanent.append("ack_sequence_order_invalid")
    matching_attempt_acks = [
        record
        for record in ack_records
        if record.get("attempt_id") == attempt_id
    ]
    for stage, declared_seq in ack_sequences.items():
        matches = [
            record
            for record in matching_attempt_acks
            if record.get("status") == stage and record.get("seq") == declared_seq
        ]
        if not matches:
            waiting.append(f"{stage}_ack_not_archived")
        elif len(matches) != 1:
            permanent.append(f"{stage}_ack_ambiguous")
        elif autonomous:
            permanent.extend(_identity_blockers(matches[0], command, f"{stage}_ack"))
    unexpected_terminal_acks = [
        record
        for record in matching_attempt_acks
        if record.get("status") in {"failed", "rejected", "skipped_stale"}
    ]
    if unexpected_terminal_acks:
        permanent.append("contradictory_terminal_ack")

    created_at = _aware_timestamp(command.get("created_at"))
    approved_at = _aware_timestamp(command.get("approved_at"))
    expires_at = _aware_timestamp(command.get("expires_at"))
    completed_at = _aware_timestamp(command.get("completed_at"))
    if created_at is None:
        permanent.append("created_at_invalid")
    if approved_at is None:
        permanent.append("approved_at_invalid")
    if expires_at is None:
        permanent.append("expires_at_invalid")
    if completed_at is None:
        permanent.append("completed_at_invalid")
    if all(value is not None for value in (created_at, approved_at, completed_at, expires_at)):
        assert created_at is not None and approved_at is not None
        assert completed_at is not None and expires_at is not None
        if not (created_at <= approved_at <= completed_at <= expires_at):
            permanent.append("command_timestamp_order_invalid")

    source: Optional[Dict[str, Any]] = source_records[0] if len(source_records) == 1 else None
    if not source_records:
        permanent.append("source_snapshot_missing")
    elif len(source_records) != 1:
        permanent.append("source_snapshot_ambiguous")
    source_seq: Optional[int] = None
    if source is not None:
        source_seq = parse_positive_sequence(source.get("seq"))
        outcome["source"]["seq"] = source_seq
        if source.get("snapshot_id") != command.get("source_snapshot_id"):
            permanent.append("source_snapshot_id_mismatch")
        if canonical_state_hash(source) != command.get("state_hash"):
            permanent.append("source_state_hash_mismatch")
        if source_seq is None:
            permanent.append("source_sequence_invalid")
        source_ts = parse_snapshot_timestamp(source.get("ts"))
        if source_ts is None:
            permanent.append("source_timestamp_invalid")
        permanent.extend(_identity_blockers(source, command, "source"))
        if source.get("save_dirty") is not False:
            permanent.append("source_save_dirty")
        if source_ts is not None:
            permanent.extend(
                f"source_{blocker}"
                for blocker in snapshot_trust_blockers(source, now=source_ts)
                if blocker != "snapshot_stale"
            )
        permanent.extend(
            _condition_blockers(
                source,
                preconditions,
                action=action,
                prefix="source_precondition",
            )
        )
        command_source = command.get("source") if isinstance(command.get("source"), dict) else {}
        if command_source.get("state_seq") is not None and command_source.get("state_seq") != source_seq:
            permanent.append("source_declared_sequence_mismatch")
        if command_source.get("state_ts") is not None and command_source.get("state_ts") != source.get("ts"):
            permanent.append("source_declared_timestamp_mismatch")

    result = command.get("result") if isinstance(command.get("result"), dict) else None
    before_state: Optional[Mapping[str, Any]] = None
    after_state: Optional[Mapping[str, Any]] = None
    if result is None:
        permanent.append("executor_result_missing")
    else:
        before_state = result.get("before_state") if isinstance(result.get("before_state"), dict) else None
        after_state = result.get("after_state") if isinstance(result.get("after_state"), dict) else None
    if before_state is None:
        permanent.append("direct_before_state_missing")
    else:
        outcome["direct"]["before_state_hash"] = canonical_state_hash(dict(before_state))
        permanent.extend(_identity_blockers(before_state, command, "direct_before"))
        if before_state.get("capture_source") != "executor_direct_read":
            permanent.append("direct_before_capture_source_invalid")
        permanent.extend(
            _condition_blockers(
                before_state,
                preconditions,
                action=action,
                prefix="direct_precondition",
            )
        )
    if after_state is None:
        permanent.append("direct_after_state_missing")
    else:
        outcome["direct"]["after_state_hash"] = canonical_state_hash(dict(after_state))
        permanent.extend(_identity_blockers(after_state, command, "direct_after"))
        if after_state.get("capture_source") != "executor_direct_read":
            permanent.append("direct_after_capture_source_invalid")
        permanent.extend(
            _condition_blockers(
                after_state,
                effects,
                action=action,
                prefix="direct_effect",
            )
        )

    if replay_mode:
        waiting.append("replay_mode_non_actionable")
    if health.get("status") != "ok" or health.get("trust_ready") is not True:
        waiting.append("bridge_not_trust_ready")
    trust_blockers = health.get("trust_blockers")
    if not isinstance(trust_blockers, list) or trust_blockers:
        waiting.append("bridge_trust_blockers_present")
    if witness is None:
        waiting.append("witness_snapshot_missing")
    else:
        witness_state = dict(witness)
        witness_seq = parse_positive_sequence(witness_state.get("seq"))
        witness_hash = canonical_state_hash(witness_state)
        outcome["witness"] = {
            "snapshot_id": witness_state.get("snapshot_id"),
            "seq": witness_seq,
            "ts": witness_state.get("ts"),
            "state_hash": witness_hash,
        }
        witness_trust_errors = snapshot_trust_blockers(witness_state)
        waiting.extend(f"witness_{blocker}" for blocker in witness_trust_errors)
        permanent.extend(_identity_blockers(witness_state, command, "witness"))
        if witness_state.get("save_dirty") is not False:
            permanent.append("witness_save_dirty")
        if witness_seq is None:
            waiting.append("witness_sequence_invalid")
        elif source_seq is not None and witness_seq <= source_seq:
            waiting.append("witness_not_newer_than_source")
        witness_at = parse_snapshot_timestamp(witness_state.get("ts"))
        if witness_at is None:
            waiting.append("witness_timestamp_invalid")
        elif completed_at is not None and witness_at < completed_at:
            waiting.append("witness_precedes_completion")
        if health.get("current_session_id") != command.get("session_id"):
            permanent.append("health_session_mismatch")
        if health.get("latest_snapshot_id") != witness_state.get("snapshot_id"):
            waiting.append("health_witness_snapshot_mismatch")
        if health.get("latest_seq") != witness_state.get("seq"):
            waiting.append("health_witness_sequence_mismatch")
        health_source = health.get("source") if isinstance(health.get("source"), Mapping) else {}
        if (
            health_source.get("session_id") != witness_state.get("session_id")
            or health_source.get("snapshot_id") != witness_state.get("snapshot_id")
            or health_source.get("state_hash") != witness_hash
        ):
            waiting.append("health_witness_source_mismatch")
        witness_is_later = (
            witness_seq is not None
            and source_seq is not None
            and witness_seq > source_seq
            and witness_at is not None
            and completed_at is not None
            and witness_at >= completed_at
            and ack_sequences.get("local_effect_observed") is not None
            and witness_seq > int(ack_sequences["local_effect_observed"] or 0)
        )
        if (
            witness_seq is not None
            and ack_sequences.get("local_effect_observed") is not None
            and witness_seq <= int(ack_sequences["local_effect_observed"] or 0)
        ):
            waiting.append("witness_not_after_local_effect_ack")
        if witness_is_later and health.get("status") == "ok" and health.get("trust_ready") is True:
            permanent.extend(
                _condition_blockers(
                    witness_state,
                    effects,
                    action=action,
                    prefix="witness_effect",
                )
            )

    permanent = sorted(set(permanent))
    waiting = sorted(set(waiting))
    if permanent:
        outcome["confirmation_status"] = CONFIRMATION_REJECTED
        outcome["classification"] = "behavior_fail"
        outcome["blockers"] = permanent + [item for item in waiting if item not in permanent]
    elif waiting:
        outcome["confirmation_status"] = CONFIRMATION_WAITING
        outcome["classification"] = None
        outcome["blockers"] = waiting
    else:
        outcome["confirmation_status"] = CONFIRMATION_CONFIRMED
        outcome["classification"] = None
        outcome["blockers"] = []
    outcome["attestation_hash"] = _hash_document(outcome, "attestation_hash")
    return outcome


def build_action_outcomes(
    bridge_dir: pathlib.Path,
    latest: Optional[Mapping[str, Any]],
    health: Mapping[str, Any],
    *,
    replay_mode: bool = False,
) -> Dict[str, Any]:
    """Build the Bridge-owned distinction between executor and snapshot proof.

    The executor's ``done/verified`` result is necessary but insufficient.  A
    command becomes ``snapshot_confirmed`` only after a later trusted Probe
    snapshot independently satisfies every declared expected effect.
    """

    bridge_dir = bridge_dir.expanduser().resolve()
    current_session = latest.get("session_id") if isinstance(latest, Mapping) else None
    grouped = _command_locations(bridge_dir)
    candidates: List[Tuple[str, str, Dict[str, Any], int]] = []
    wanted_ids: List[str] = []
    attest_ids: List[str] = []
    for command_id, locations in sorted(grouped.items()):
        current_locations = [item for item in locations if item[2].get("session_id") == current_session]
        if not current_locations:
            continue
        directory_name, _, command = current_locations[0]
        terminal_locations = [
            item for item in current_locations if item[0] in {"confirming", "done"}
        ]
        if not terminal_locations:
            continue
        directory_name, _, command = sorted(
            terminal_locations,
            key=lambda item: 0 if item[0] == "done" else 1,
        )[0]
        # noop/focus_note have no game-state effect to witness.  The executor
        # completes them directly in done/ and they must not create rejected
        # snapshot attestations or engage the mutation confirmation pipeline.
        if command.get("action") in NON_MUTATING_ACTIONS:
            continue
        candidates.append((command_id, directory_name, command, len(locations)))
        if _stored_confirmation(command, directory_name=directory_name, location_count=len(locations)):
            # An intact stored attestation is returned as is; its source
            # snapshot and acks are never read (review L7).
            continue
        attest_ids.append(command_id)
        source_snapshot_id = command.get("source_snapshot_id")
        if isinstance(source_snapshot_id, str):
            wanted_ids.append(source_snapshot_id)

    sources = (
        _source_snapshots(bridge_dir, str(current_session), wanted_ids)
        if isinstance(current_session, str) and current_session
        else {}
    )
    acks = (
        _command_acks(
            bridge_dir,
            str(current_session),
            attest_ids,
        )
        if isinstance(current_session, str) and current_session
        else {}
    )
    outcomes = [
        _attest_done_command(
            command,
            directory_name=directory_name,
            location_count=location_count,
            source_records=sources.get(str(command.get("source_snapshot_id") or ""), []),
            ack_records=acks.get(str(command.get("id") or ""), []),
            witness=latest,
            health=health,
            replay_mode=replay_mode,
        )
        for _, directory_name, command, location_count in candidates
    ]
    counts = {
        CONFIRMATION_EXECUTED: len(outcomes),
        CONFIRMATION_WAITING: sum(
            item.get("confirmation_status") == CONFIRMATION_WAITING for item in outcomes
        ),
        CONFIRMATION_CONFIRMED: sum(
            item.get("confirmation_status") == CONFIRMATION_CONFIRMED for item in outcomes
        ),
        CONFIRMATION_REJECTED: sum(
            item.get("confirmation_status") == CONFIRMATION_REJECTED for item in outcomes
        ),
    }
    source = {
        "session_id": current_session,
        "save_id_hash": latest.get("save_id_hash") if isinstance(latest, Mapping) else None,
        "world_id": latest.get("world_id") if isinstance(latest, Mapping) else None,
        "snapshot_id": latest.get("snapshot_id") if isinstance(latest, Mapping) else None,
        "seq": latest.get("seq") if isinstance(latest, Mapping) else None,
        "state_hash": canonical_state_hash(dict(latest)) if isinstance(latest, Mapping) else None,
    }
    trust_blockers = list(health.get("trust_blockers") or []) if isinstance(health, Mapping) else []
    document: Dict[str, Any] = {
        "schema": ACTION_OUTCOMES_SCHEMA,
        "updated_at": _now_iso(),
        "source": source,
        "trust": {
            "status": "trusted"
            if not replay_mode and health.get("status") == "ok" and health.get("trust_ready") is True
            else "untrusted",
            "blockers": sorted(set(str(item) for item in trust_blockers)),
            "replay_mode": replay_mode,
        },
        "counts": counts,
        "outcomes": outcomes,
        "outcomes_hash": None,
    }
    document["outcomes_hash"] = _hash_document(document, "outcomes_hash")
    return document


def validate_action_outcomes(document: Mapping[str, Any]) -> None:
    if document.get("schema") != ACTION_OUTCOMES_SCHEMA:
        raise ActionOutcomeError("action outcomes schema is invalid")
    declared_hash = document.get("outcomes_hash")
    if not isinstance(declared_hash, str) or _HASH_RE.fullmatch(declared_hash) is None:
        raise ActionOutcomeError("action outcomes hash is invalid")
    if declared_hash != _hash_document(document, "outcomes_hash"):
        raise ActionOutcomeError("action outcomes hash does not match its payload")
    outcomes = document.get("outcomes")
    if not isinstance(outcomes, list):
        raise ActionOutcomeError("action outcomes list is invalid")
    identifiers: set[str] = set()
    for outcome in outcomes:
        if not isinstance(outcome, Mapping) or outcome.get("schema") != ACTION_OUTCOME_SCHEMA:
            raise ActionOutcomeError("action outcome entry is invalid")
        command_id = outcome.get("command_id")
        if not isinstance(command_id, str) or not command_id or command_id in identifiers:
            raise ActionOutcomeError("action outcome command id is invalid or duplicated")
        identifiers.add(command_id)
        declared_attestation = outcome.get("attestation_hash")
        if (
            not isinstance(declared_attestation, str)
            or _HASH_RE.fullmatch(declared_attestation) is None
            or declared_attestation != _hash_document(outcome, "attestation_hash")
        ):
            raise ActionOutcomeError("action outcome attestation hash is invalid")
        if outcome.get("confirmation_status") not in {
            CONFIRMATION_WAITING,
            CONFIRMATION_CONFIRMED,
            CONFIRMATION_REJECTED,
        }:
            raise ActionOutcomeError("action outcome confirmation status is invalid")
        blockers = outcome.get("blockers")
        if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
            raise ActionOutcomeError("action outcome blockers are invalid")
        if outcome.get("confirmation_status") == CONFIRMATION_CONFIRMED and blockers:
            raise ActionOutcomeError("confirmed action outcome contains blockers")


def _confirmed_command_payload(
    command: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    if outcome.get("confirmation_status") != CONFIRMATION_CONFIRMED:
        return None
    if outcome.get("command_location") != "confirming":
        return None
    if command.get("status") not in {"executed_pending_snapshot", "confirmed"}:
        return None
    if command_intent_hash(command) != outcome.get("command_intent_hash"):
        return None
    witness = outcome.get("witness") if isinstance(outcome.get("witness"), Mapping) else {}
    confirmed_at = witness.get("ts")
    if not isinstance(confirmed_at, str) or _aware_timestamp(confirmed_at) is None:
        return None
    payload = copy.deepcopy(dict(command))
    payload["status"] = "confirmed"
    payload["confirmed_at"] = confirmed_at
    payload["confirmation"] = copy.deepcopy(dict(outcome))
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    result.update(
        {
            "confirmed_snapshot_id": witness.get("snapshot_id"),
            "confirmed_seq": witness.get("seq"),
            "confirmed_state_hash": witness.get("state_hash"),
            "attestation_hash": outcome.get("attestation_hash"),
        }
    )
    payload["result"] = result
    return payload


def confirmed_command_commit_blockers(
    command: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> List[str]:
    """Validate that a unique ``done`` command is the commit of ``outcome``."""

    blockers: List[str] = []
    if command.get("status") != "confirmed":
        blockers.append("done_status_not_confirmed")
    if outcome.get("confirmation_status") != CONFIRMATION_CONFIRMED:
        blockers.append("certificate_not_confirmed")
    if outcome.get("command_location") != "confirming":
        blockers.append("certificate_origin_not_confirming")
    if command_intent_hash(command) != outcome.get("command_intent_hash"):
        blockers.append("done_command_intent_hash_mismatch")
    confirmation = command.get("confirmation")
    if not isinstance(confirmation, Mapping) or not _strict_json_equal(
        dict(confirmation), dict(outcome)
    ):
        blockers.append("done_embedded_confirmation_mismatch")

    witness = outcome.get("witness") if isinstance(outcome.get("witness"), Mapping) else {}
    if command.get("confirmed_at") != witness.get("ts"):
        blockers.append("done_confirmation_timestamp_mismatch")
    result = command.get("result") if isinstance(command.get("result"), Mapping) else {}
    expected_result = {
        "confirmed_snapshot_id": witness.get("snapshot_id"),
        "confirmed_seq": witness.get("seq"),
        "confirmed_state_hash": witness.get("state_hash"),
        "attestation_hash": outcome.get("attestation_hash"),
    }
    for field, expected in expected_result.items():
        if not _strict_json_equal(result.get(field), expected):
            blockers.append(f"done_result_{field}_mismatch")

    direct = outcome.get("direct") if isinstance(outcome.get("direct"), Mapping) else {}
    if command.get("completed_at") != direct.get("completed_at"):
        blockers.append("done_completed_at_mismatch")
    before_state = result.get("before_state") if isinstance(result.get("before_state"), Mapping) else None
    after_state = result.get("after_state") if isinstance(result.get("after_state"), Mapping) else None
    if before_state is None or canonical_state_hash(dict(before_state)) != direct.get("before_state_hash"):
        blockers.append("done_direct_before_state_hash_mismatch")
    if after_state is None or canonical_state_hash(dict(after_state)) != direct.get("after_state_hash"):
        blockers.append("done_direct_after_state_hash_mismatch")
    return sorted(set(blockers))


def _matching_published_documents(
    document: Mapping[str, Any],
    paths: Sequence[pathlib.Path],
) -> bool:
    expected = dict(document)
    for path in paths:
        published = _read_json_object(path)
        if published is None:
            return False
        try:
            validate_action_outcomes(published)
        except ActionOutcomeError:
            return False
        if not _strict_json_equal(published, expected):
            return False
    return True


def _commit_publication_paths(
    bridge_dir: pathlib.Path,
    document: Mapping[str, Any],
) -> Optional[Tuple[pathlib.Path, pathlib.Path]]:
    source = document.get("source") if isinstance(document.get("source"), Mapping) else {}
    session_id = source.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    root_path = bridge_dir / "action-outcomes.json"
    session_path = bridge_dir / "sessions" / _safe_session_name(session_id) / "action-outcomes.json"
    return root_path, session_path


def stage_confirmed_outcomes_commit(
    bridge_dir: pathlib.Path,
    document: Mapping[str, Any],
) -> Dict[str, Any]:
    """Publish a recovery journal only after both Bridge output copies agree."""

    validate_action_outcomes(document)
    bridge_dir = bridge_dir.expanduser().resolve()
    paths = _commit_publication_paths(bridge_dir, document)
    if paths is None or not _matching_published_documents(document, paths):
        raise ActionOutcomeError("root and session action outcomes are not identically published")
    session_id = str(document["source"]["session_id"])
    record: Dict[str, Any] = {
        "schema": ACTION_OUTCOMES_COMMIT_SCHEMA,
        "outcomes_hash": document.get("outcomes_hash"),
        "session_id": session_id,
        "root_certificate": "action-outcomes.json",
        "session_certificate": (
            pathlib.PurePosixPath("sessions")
            / _safe_session_name(session_id)
            / "action-outcomes.json"
        ).as_posix(),
        "staged_at": _now_iso(),
        "transaction_hash": None,
    }
    record["transaction_hash"] = _hash_document(record, "transaction_hash")
    write_json_atomic(bridge_dir / "action-outcomes-commit.json", record)
    return record


def _validated_staged_document(bridge_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    record = _read_json_object(bridge_dir / "action-outcomes-commit.json")
    if not isinstance(record, dict):
        return None
    required = {
        "schema",
        "outcomes_hash",
        "session_id",
        "root_certificate",
        "session_certificate",
        "staged_at",
        "transaction_hash",
    }
    if set(record) != required or record.get("schema") != ACTION_OUTCOMES_COMMIT_SCHEMA:
        return None
    transaction_hash = record.get("transaction_hash")
    if (
        not isinstance(transaction_hash, str)
        or _HASH_RE.fullmatch(transaction_hash) is None
        or transaction_hash != _hash_document(record, "transaction_hash")
    ):
        return None
    if _aware_timestamp(record.get("staged_at")) is None:
        return None
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    expected_session_certificate = (
        pathlib.PurePosixPath("sessions")
        / _safe_session_name(session_id)
        / "action-outcomes.json"
    ).as_posix()
    if (
        record.get("root_certificate") != "action-outcomes.json"
        or record.get("session_certificate") != expected_session_certificate
    ):
        return None
    root_path = bridge_dir / "action-outcomes.json"
    session_path = bridge_dir / pathlib.PurePosixPath(expected_session_certificate)
    document = _read_json_object(root_path)
    session_document = _read_json_object(session_path)
    if document is None or session_document is None:
        return None
    try:
        validate_action_outcomes(document)
        validate_action_outcomes(session_document)
    except ActionOutcomeError:
        return None
    source = document.get("source") if isinstance(document.get("source"), Mapping) else {}
    if (
        document.get("outcomes_hash") != record.get("outcomes_hash")
        or source.get("session_id") != session_id
        or not _strict_json_equal(document, session_document)
    ):
        return None
    return document


def _is_retryable_move_error(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or exc.errno in _RETRYABLE_MOVE_ERRNOS
        or getattr(exc, "winerror", None) in _RETRYABLE_MOVE_WINERRORS
    )


def _rename_with_retry(source: pathlib.Path, target: pathlib.Path) -> None:
    delay = _MOVE_INITIAL_DELAY_SECONDS
    for attempt in range(_MOVE_ATTEMPTS):
        try:
            source.rename(target)
            return
        except OSError as exc:
            if attempt + 1 >= _MOVE_ATTEMPTS or not _is_retryable_move_error(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, _MOVE_MAX_DELAY_SECONDS)


def _source_unchanged(path: pathlib.Path, expected: Mapping[str, Any]) -> bool:
    """Whether ``path`` still holds exactly the command read by the scan."""

    try:
        raw, _identity, _mtime_ns = read_stable_bytes(path, label="confirming command")
        current = loads_strict(raw.decode("utf-8-sig"))
    except (CommandCustodyError, ValueError, UnicodeError):
        return False
    return isinstance(current, dict) and _strict_json_equal(current, dict(expected))


def commit_confirmed_outcomes(
    bridge_dir: pathlib.Path,
    document: Mapping[str, Any],
    *,
    publication_paths: Optional[Sequence[pathlib.Path]] = None,
) -> List[str]:
    """Commit Bridge-confirmed commands only after the certificate is published.

    The caller must first atomically publish ``document``.  This function then
    prepares each matching ``confirming`` command in place and then atomically
    renames it to ``done`` with the immutable certificate embedded.  A crash
    between preparation and rename is recovered from the staged publication
    journal without rebuilding or weakening the certificate.

    These are queue transitions, so they run under the shared lifecycle
    custody like every other Python queue writer, and each write, rename or
    unlink first proves the source still holds the scanned command; a source
    replaced after the scan is left untouched and not reported committed
    (code review 2026-09-26, L10 follow-up).
    """

    validate_action_outcomes(document)
    bridge_dir = bridge_dir.expanduser().resolve()
    with lifecycle_custody(bridge_dir):
        return _commit_confirmed_outcomes_locked(bridge_dir, document, publication_paths=publication_paths)


def _commit_confirmed_outcomes_locked(
    bridge_dir: pathlib.Path,
    document: Mapping[str, Any],
    *,
    publication_paths: Optional[Sequence[pathlib.Path]] = None,
) -> List[str]:
    dirs = ensure_command_dirs(bridge_dir)
    expected_publications = tuple(publication_paths or (bridge_dir / "action-outcomes.json",))
    if not _matching_published_documents(document, expected_publications):
        return []
    committed: List[str] = []
    grouped = _command_locations(bridge_dir)

    # A confirmation that is definitively rejected means the mutation was
    # dispatched but its declared effect never appeared in the witness snapshot.
    # There was no terminal disposition for that case, so the command stayed in
    # ``confirming`` forever and latched the pipeline: the frame's propose gate
    # reported COMMAND_PIPELINE_MUTATION_BLOCKED and the model loop could never
    # propose again (2026-10-26 round 34).  Rejecting it is the same kind of
    # queue transition as confirming it, so it runs under the same custody and
    # under the same prove-the-source-unchanged rule.
    for raw_outcome in document.get("outcomes") or []:
        if (
            not isinstance(raw_outcome, Mapping)
            or raw_outcome.get("confirmation_status") != CONFIRMATION_REJECTED
            or raw_outcome.get("command_location") != "confirming"
        ):
            continue
        command_id = str(raw_outcome.get("command_id") or "")
        locations = grouped.get(command_id, [])
        confirming = [item for item in locations if item[0] == "confirming"]
        if len(locations) != 1 or len(confirming) != 1:
            continue
        source_path = confirming[0][1]
        command = confirming[0][2]
        if command.get("status") not in {"executed_pending_snapshot", "confirmed"}:
            continue
        if command_intent_hash(command) != raw_outcome.get("command_intent_hash"):
            continue
        target_path = dirs["rejected"] / f"{command_id}.json"
        if target_path.exists():
            continue
        if not _source_unchanged(source_path, command):
            continue
        payload = copy.deepcopy(dict(command))
        payload["status"] = "rejected"
        payload["rejected_at"] = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
        payload["rejection_reason"] = "confirmation_rejected"
        payload["confirmation"] = copy.deepcopy(dict(raw_outcome))
        write_json_atomic(target_path, payload)
        try:
            source_path.unlink()
        except FileNotFoundError:
            pass
        committed.append(command_id)

    for raw_outcome in document.get("outcomes") or []:
        if (
            not isinstance(raw_outcome, Mapping)
            or raw_outcome.get("confirmation_status") != CONFIRMATION_CONFIRMED
            or raw_outcome.get("command_location") != "confirming"
        ):
            continue
        command_id = str(raw_outcome.get("command_id") or "")
        locations = grouped.get(command_id, [])
        confirming = [item for item in locations if item[0] == "confirming"]
        done = [item for item in locations if item[0] == "done"]
        target_path = dirs["done"] / f"{command_id}.json"

        # Recover the legacy write-done-then-unlink window, but only when the
        # two files are the exact same committed payload.  Any conflict or
        # third location remains fail-closed for operator inspection.
        if len(done) == 1:
            existing = done[0][2]
            if confirmed_command_commit_blockers(existing, raw_outcome):
                continue
            if existing.get("action") in _APPROVAL_BUNDLE_ACTIONS:
                # Recovery and idempotent re-entry must prove that the
                # command-local authority bundle exists and is identical.
                persist_confirmed_command_approval_bundle(bridge_dir, existing)
            if len(locations) == 1:
                committed.append(command_id)
                continue
            if len(locations) != 2 or len(confirming) != 1:
                continue
            source_path = confirming[0][1]
            source_command = confirming[0][2]
            expected = _confirmed_command_payload(source_command, raw_outcome)
            if expected is None or not _strict_json_equal(existing, expected):
                continue
            if not _source_unchanged(source_path, source_command):
                continue
            try:
                source_path.unlink()
            except FileNotFoundError:
                pass
            committed.append(command_id)
            continue

        if len(locations) != 1 or len(confirming) != 1:
            continue
        source_path = confirming[0][1]
        command = confirming[0][2]
        payload = _confirmed_command_payload(command, raw_outcome)
        if payload is None:
            continue
        if not _source_unchanged(source_path, command):
            continue
        if payload.get("action") in _APPROVAL_BUNDLE_ACTIONS:
            # Publish approval authority before preparing or renaming the
            # confirming command.  A missing/pruned frame, injected I/O
            # failure, or conflicting create-only bundle raises here and
            # deliberately leaves the command in confirming.
            persist_confirmed_command_approval_bundle(bridge_dir, payload)
        if command.get("status") == "confirmed":
            if not _strict_json_equal(command, payload):
                continue
        else:
            if not _source_unchanged(source_path, command):
                continue
            write_json_atomic(source_path, payload)
        if target_path.exists():
            # Never overwrite a conflicting done file.
            existing = _read_json_object(target_path)
            if existing is None or not _strict_json_equal(existing, payload):
                continue
            if not _source_unchanged(source_path, payload):
                continue
            try:
                source_path.unlink()
            except FileNotFoundError:
                pass
            committed.append(command_id)
            continue
        if not _source_unchanged(source_path, payload):
            continue
        try:
            _rename_with_retry(source_path, target_path)
        except FileNotFoundError:
            existing = _read_json_object(target_path)
            if existing is None or not _strict_json_equal(existing, payload):
                continue
        final = _read_json_object(target_path)
        if final is None or confirmed_command_commit_blockers(final, raw_outcome):
            continue
        committed.append(command_id)
    return committed


def recover_staged_confirmed_outcomes(bridge_dir: pathlib.Path) -> List[str]:
    """Idempotently finish the latest root/session-published confirmation transaction."""

    bridge_dir = bridge_dir.expanduser().resolve()
    with lifecycle_custody(bridge_dir):
        return _recover_staged_confirmed_outcomes_locked(bridge_dir)


def _recover_staged_confirmed_outcomes_locked(bridge_dir: pathlib.Path) -> List[str]:
    document = _validated_staged_document(bridge_dir)
    if document is None:
        prepared = _prepared_confirming_command_ids(bridge_dir)
        if prepared:
            raise ActionOutcomeError(
                "prepared confirming commands require a valid root/session commit journal: "
                + ",".join(prepared)
            )
        return []
    paths = _commit_publication_paths(bridge_dir, document)
    if paths is None:
        return []
    committed = commit_confirmed_outcomes(
        bridge_dir,
        document,
        publication_paths=paths,
    )
    prepared = _prepared_confirming_command_ids(bridge_dir)
    if prepared:
        raise ActionOutcomeError(
            "prepared confirming commands could not be committed without conflict: "
            + ",".join(prepared)
        )
    return committed


def _prepared_confirming_command_ids(bridge_dir: pathlib.Path) -> List[str]:
    prepared: List[str] = []
    confirming = ensure_command_dirs(bridge_dir)["confirming"]
    for path in sorted(confirming.glob("*.json")):
        command = _read_json_object(path)
        if (
            isinstance(command, dict)
            and command.get("status") == "confirmed"
            and isinstance(command.get("confirmation"), Mapping)
        ):
            prepared.append(str(command.get("id") or path.stem))
    return sorted(set(prepared))


def read_action_outcome(
    bridge_dir: pathlib.Path,
    command_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    path = bridge_dir.expanduser().resolve() / "action-outcomes.json"
    document = _read_json_object(path)
    if document is None:
        return None, ["action_outcomes_unavailable"]
    try:
        validate_action_outcomes(document)
    except ActionOutcomeError:
        return None, ["action_outcomes_invalid"]
    matches = [
        dict(item)
        for item in document.get("outcomes") or []
        if isinstance(item, Mapping) and item.get("command_id") == command_id
    ]
    if not matches:
        return None, ["action_outcome_missing"]
    if len(matches) != 1:
        return None, ["action_outcome_ambiguous"]
    return matches[0], []


__all__ = [
    "ACTION_OUTCOMES_COMMIT_SCHEMA",
    "ACTION_OUTCOMES_SCHEMA",
    "ACTION_OUTCOME_SCHEMA",
    "CONFIRMED_COMMAND_APPROVAL_BUNDLE_SCHEMA",
    "CONFIRMATION_CONFIRMED",
    "CONFIRMATION_REJECTED",
    "CONFIRMATION_WAITING",
    "ActionOutcomeError",
    "build_action_outcomes",
    "commit_confirmed_outcomes",
    "command_intent_hash",
    "confirmed_command_approval_bundle_path",
    "confirmed_command_commit_blockers",
    "load_confirmed_command_approval_bundle",
    "persist_confirmed_command_approval_bundle",
    "read_action_outcome",
    "recover_staged_confirmed_outcomes",
    "stage_confirmed_outcomes_commit",
    "validate_action_outcomes",
    "validate_confirmed_command_approval_bundle",
]
