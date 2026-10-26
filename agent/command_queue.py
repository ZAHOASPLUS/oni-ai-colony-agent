from __future__ import annotations

import datetime as dt
import copy
import errno
import hashlib
import json
import math
import os
import pathlib
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from agent.command_custody import (
    CommandCustodyConflict,
    CommandCustodyError,
    approval_recovery_blockers,
    lifecycle_custody,
    lifecycle_recovery_blockers,
    publish_pending_command,
    read_stable_bytes,
    recover_lifecycle_transactions,
    transition_pending_envelope,
)
from agent.action_policy import (
    ALLOWED_ACTIONS as ACTION_POLICY_ALLOWED_ACTIONS,
    ACTION_POLICY_SCHEMA,
    AUTONOMOUS_ACTION_POLICY_SCHEMA,
    action_policy_hash,
    action_policy_is_valid,
    autonomous_identifier_is_valid,
    autonomous_policy_state_blockers,
)
from agent.canonical_hash import CanonicalHashError, canonical_hash as canonical_document_hash
from agent.decision import DecisionFrameError, validate_decision_frame
from agent.field_contracts import (
    NON_MUTATING_ACTIONS,
    condition_contract_blockers,
    condition_declaration_blockers,
)
from agent.state_quality import canonical_state_hash, parse_timestamp, safe_get


COMMAND_SCHEMA = "oni.command.v3"
AUTONOMOUS_COMMAND_SCHEMA = "oni.command.v4"
AUTONOMOUS_PENDING_STATUS = "pending_autonomous_authorization"
COMMAND_DIRS = (
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
COMMAND_PIPELINE_SCHEMA = "oni.command-pipeline-gate.v1"
COMMAND_PIPELINE_ACTIVE_STAGES = (
    "pending",
    "approving",
    "approved",
    "executing",
    "confirming",
)
COMMAND_PIPELINE_SCAN_STAGES = COMMAND_PIPELINE_ACTIVE_STAGES + ("failed",)
OPERATOR_PENDING_MODES = {"runtime_validation", "planned"}
MODEL_SELECTED_MODE = "model_selected"
# Pending modes approved against their immutable archived source frame: they
# expire only by TTL and latch further mutation proposals while pending.
# Model-selected commands are Bridge-published (agent.model_loop) but bound the
# same way, so a human can approve them across snapshots (review 2026-09-26, M1).
ARCHIVED_FRAME_PENDING_MODES = OPERATOR_PENDING_MODES | {MODEL_SELECTED_MODE}
# A failed mutation is a latch unless the executor proved it stopped before
# dispatch (docs/protocol.md) or an operator explicitly reconciled it.
_DEFINITE_FAILURE_CLASSIFICATIONS = frozenset({"behavior_fail", "rejected", "runtime_fail"})
OPERATOR_RESOLUTION_SCHEMA = "oni.operator-resolution.v1"
OPERATOR_RESOLUTION_OUTCOMES = frozenset({"effect_present", "effect_absent"})
ALLOWED_ACTIONS = set(ACTION_POLICY_ALLOWED_ACTIONS)
DEFAULT_TTL_SECONDS = 30
DEFAULT_MAX_PENDING = 20
# Live run m1-0927c (2026-09-27): while the game ran, every snapshot
# superseded the source binding of the pending idle_dupes focus_note, so it
# was rejected as stale and re-issued once per snapshot (29 rejected records
# in five minutes).  After a rejection caused by a changed game state a
# non-mutating suggestion for the same incident is re-issued at most once per
# this window.  A frame-only rejection is not throttled: publishing a
# suggestion changes the pipeline projection of the next decision frame, so
# every new suggestion is re-bound once (pending_source_frame_stale) before it
# settles, and throttling that step would leave nothing pending.  Mutating
# survival suggestions (the arbitrated pause) keep their per-generation refresh.
NON_MUTATING_RESUGGEST_SECONDS = 120
SOURCE_STALE_REJECTION_REASONS = frozenset(
    {
        "pending_source_snapshot_stale",
        "pending_source_state_hash_stale",
    }
)
_ATOMIC_REPLACE_ATTEMPTS = 8
_ATOMIC_REPLACE_INITIAL_DELAY_SECONDS = 0.025
_ATOMIC_REPLACE_MAX_DELAY_SECONDS = 0.2
_RETRYABLE_REPLACE_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
_RETRYABLE_REPLACE_WINERRORS = {5, 32, 33}
_COMMAND_INTENT_FIELDS = (
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


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def ensure_command_dirs(bridge_dir: pathlib.Path) -> Dict[str, pathlib.Path]:
    commands_root = bridge_dir / "commands"
    result: Dict[str, pathlib.Path] = {}
    for name in COMMAND_DIRS:
        path = commands_root / name
        path.mkdir(parents=True, exist_ok=True)
        result[name] = path
    return result


def load_policy(bridge_dir: pathlib.Path) -> Dict[str, Any]:
    path = bridge_dir / "action-policy.json"
    if not path.exists():
        return {
            "schema": "oni.action-policy.v1",
            "actions_enabled": False,
            "suggestions_enabled": True,
            "allowed_actions": [],
            "allowed_save_hashes": [],
            "command_ttl_seconds": DEFAULT_TTL_SECONDS,
            "max_pending_commands": DEFAULT_MAX_PENDING,
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema": "oni.action-policy.v1",
            "actions_enabled": False,
            "suggestions_enabled": True,
            "allowed_actions": [],
            "allowed_save_hashes": [],
            "command_ttl_seconds": DEFAULT_TTL_SECONDS,
            "max_pending_commands": DEFAULT_MAX_PENDING,
            "policy_error": "invalid_json",
        }
    return value if isinstance(value, dict) else {}


def command_pipeline_readiness(
    bridge_dir: pathlib.Path,
    *,
    latest_state: Optional[Mapping[str, Any]] = None,
    proposal_frame: Optional[Mapping[str, Any]] = None,
    policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Inspect the durable command pipeline without consulting the game runtime.

    The result deliberately separates proposal blocking from confirmation
    processing.  A valid mutating command in ``confirming`` blocks another
    mutation proposal while remaining a valid confirmation candidate.  A
    structurally valid, unexpired operator/planned command in ``pending`` also
    latches proposals even if its historical frame or policy is no longer
    current; operator custody is never silently arbitrated by suggestion sync.
    The non-mutating ``noop`` and ``focus_note`` actions are reported but never
    engage either mutation gate.
    """

    bridge_dir = bridge_dir.expanduser().resolve()
    dirs = ensure_command_dirs(bridge_dir)
    frame_valid = False
    if proposal_frame is not None:
        try:
            validate_decision_frame(proposal_frame)
            frame_valid = True
        except (DecisionFrameError, TypeError, ValueError, OverflowError):
            frame_valid = False
    proposal_frame_id = (
        proposal_frame.get("frame_id")
        if isinstance(proposal_frame, Mapping)
        and isinstance(proposal_frame.get("frame_id"), str)
        else None
    )
    try:
        # DecisionFrame represents a missing latest snapshot with the
        # canonical hash of an empty state object.  Mirror that exact blocked
        # identity here so Bridge can publish a bound null ControlPacket while
        # still rejecting every action candidate.
        evaluated_state_hash = canonical_state_hash(
            dict(latest_state) if isinstance(latest_state, Mapping) else {}
        )
    except (TypeError, ValueError, OverflowError):
        evaluated_state_hash = None
    evaluation_context = {
        "phase": (
            "pre_frame"
            if proposal_frame is None
            else "frame_bound"
            if frame_valid
            else "invalid_frame"
        ),
        "proposal_frame_id": proposal_frame_id,
        "state_hash": evaluated_state_hash,
        "policy_hash": _policy_hash(policy),
    }
    records: List[Dict[str, Any]] = []
    invalid_records: List[Dict[str, str]] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    counts = {stage: 0 for stage in COMMAND_PIPELINE_SCAN_STAGES}

    for stage in COMMAND_PIPELINE_SCAN_STAGES:
        for path in sorted(dirs[stage].glob("*.json"), key=lambda item: item.name):
            try:
                raw, _identity_value, _mtime = read_stable_bytes(
                    path,
                    label=f"commands/{stage}/{path.name}",
                )
                value = json.loads(
                    raw.decode("utf-8-sig"),
                    parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
                )
            except (CommandCustodyError, UnicodeError, json.JSONDecodeError, ValueError):
                invalid_records.append(
                    {
                        "stage": stage,
                        "path": f"commands/{stage}/{path.name}",
                        "reason": "command_file_unreadable",
                    }
                )
                continue
            if not isinstance(value, dict):
                invalid_records.append(
                    {
                        "stage": stage,
                        "path": f"commands/{stage}/{path.name}",
                        "reason": "command_object_invalid",
                    }
                )
                continue
            if stage == "failed" and not _is_unresolved_outcome_unknown(value):
                continue

            command_id = value.get("id")
            if not isinstance(command_id, str) or not command_id:
                invalid_records.append(
                    {
                        "stage": stage,
                        "path": f"commands/{stage}/{path.name}",
                        "reason": "command_id_invalid",
                    }
                )
                continue
            if stage == "pending" and path.name != f"{command_id}.json":
                # Approval and claim tools address pending commands by their
                # immutable id.  A readable envelope under any other basename
                # is not actionable and must not be advertised as a candidate.
                invalid_records.append(
                    {
                        "stage": stage,
                        "path": f"commands/{stage}/{path.name}",
                        "reason": "command_path_noncanonical",
                    }
                )
                continue
            action = value.get("action")
            action_name = action if isinstance(action, str) and action else "unknown"
            mutating = action_name not in NON_MUTATING_ACTIONS
            classification = _pipeline_classification(stage, value, mutating=mutating)
            record = {
                "command_id": command_id,
                "action": action_name,
                "stage": stage,
                "status": value.get("status"),
                "classification": classification,
                "mutating": mutating,
                "path": f"commands/{stage}/{path.name}",
                "intent_hash": _command_intent_hash(value),
                "command": value,
            }
            counts[stage] += 1
            records.append(record)
            grouped.setdefault(command_id, []).append(record)

    # Active/failed records drive the public pipeline view.  Terminal stages
    # are also indexed by command id so an old rejected/quarantined/done
    # envelope cannot silently coexist with a newly published command bearing
    # the same id.  Keep those records out of counts and ControlPacket input:
    # they are identity evidence, not executable candidates.
    identity_grouped: Dict[str, List[Dict[str, Any]]] = {
        command_id: list(items) for command_id, items in grouped.items()
    }
    for stage in ("done", "rejected", "quarantine"):
        for path in sorted(dirs[stage].glob("*.json"), key=lambda item: item.name):
            try:
                raw, _identity_value, _mtime = read_stable_bytes(
                    path,
                    label=f"commands/{stage}/{path.name}",
                )
                value = json.loads(raw.decode("utf-8-sig"), parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
            except (CommandCustodyError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            command_id = value.get("id")
            if not isinstance(command_id, str) or not command_id:
                continue
            action = value.get("action")
            action_name = action if isinstance(action, str) and action else "unknown"
            identity_grouped.setdefault(command_id, []).append(
                {
                    "command_id": command_id,
                    "action": action_name,
                    "stage": stage,
                    "status": value.get("status"),
                    "classification": "terminal_identity",
                    "mutating": action_name not in NON_MUTATING_ACTIONS,
                    "path": f"commands/{stage}/{path.name}",
                    "intent_hash": _command_intent_hash(value),
                    "command": value,
                    "identity_only": True,
                }
            )

    proposal_blockers: List[str] = []
    confirmation_blockers: List[str] = []
    ambiguous_outcomes: List[Dict[str, Any]] = []
    duplicate_ids: List[Dict[str, Any]] = []
    stale_pending: List[Dict[str, Any]] = []

    for item in invalid_records:
        blocker = f"{item['reason']}:{item['stage']}:{pathlib.Path(item['path']).name}"
        proposal_blockers.append(blocker)
        confirmation_blockers.append(blocker)

    for recovery_name in lifecycle_recovery_blockers(bridge_dir):
        blocker = f"lifecycle_recovery_unresolved:{recovery_name}"
        proposal_blockers.append(blocker)
        confirmation_blockers.append(blocker)
        ambiguous_outcomes.append(
            {
                "command_id": recovery_name,
                "classification": "lifecycle_recovery_unknown",
                "stages": ["lifecycle-recovery"],
                "mutating": True,
            }
        )

    for recovery_name in approval_recovery_blockers(bridge_dir):
        blocker = f"approval_recovery_unresolved:{recovery_name}"
        proposal_blockers.append(blocker)
        confirmation_blockers.append(blocker)
        ambiguous_outcomes.append(
            {"command_id": recovery_name, "classification": "approval_recovery_unknown", "stages": ["approval-recovery"], "mutating": True}
        )

    for command_id, locations in sorted(identity_grouped.items()):
        if len(locations) <= 1:
            continue
        intent_hashes = sorted({str(item["intent_hash"]) for item in locations})
        mutating = any(bool(item["mutating"]) for item in locations)
        duplicate = {
            "command_id": command_id,
            "stages": [item["stage"] for item in sorted(locations, key=_record_sort_key)],
            "paths": [item["path"] for item in sorted(locations, key=_record_sort_key)],
            "conflicting_intents": len(intent_hashes) > 1,
            "mutating": mutating,
        }
        duplicate_ids.append(duplicate)
        ambiguous_outcomes.append(
            {
                "command_id": command_id,
                "classification": (
                    "conflicting_duplicate_command_id"
                    if duplicate["conflicting_intents"]
                    else "duplicate_command_id"
                ),
                "stages": duplicate["stages"],
                "mutating": mutating,
            }
        )
        if mutating:
            duplicate_blocker = f"duplicate_command_id:{command_id}"
            proposal_blockers.append(duplicate_blocker)
            confirmation_blockers.append(duplicate_blocker)
            if duplicate["conflicting_intents"]:
                conflict_blocker = f"conflicting_command_intent:{command_id}"
                proposal_blockers.append(conflict_blocker)
                confirmation_blockers.append(conflict_blocker)

    for record in sorted(records, key=_record_sort_key):
        command_id = str(record["command_id"])
        mutating = bool(record["mutating"])
        classification = str(record["classification"])
        if record["stage"] == "pending":
            raw_mode = record["command"].get("mode")
            mode = raw_mode if isinstance(raw_mode, str) else ""
            if mutating and mode not in ARCHIVED_FRAME_PENDING_MODES | {"suggested"}:
                # A readable mutating envelope with no supported owner must not
                # disappear or be treated as queue capacity.  Keep it in place
                # for explicit operator inspection and fail proposal closed.
                proposal_blockers.append(f"mutation_pending_mode_invalid:{command_id}")
                continue
            if mutating and mode in ARCHIVED_FRAME_PENDING_MODES:
                owner = "model_selection" if mode == MODEL_SELECTED_MODE else "operator"
                if _operator_pending_mutation_latches(record["command"]):
                    # Operator-created runtime and Colony Plan commands (and
                    # model-selected commands) retain approval authority
                    # through their immutable archived source frame.  A moving
                    # current frame/policy therefore cannot make them safe to
                    # ignore or auto-replace.  TTL expiry remains the sole
                    # automatic pending disposition.
                    proposal_blockers.append(f"mutation_pending_{owner}:{command_id}")
                else:
                    # A supported owner label is not enough to make a command
                    # approval-capable.  Malformed envelopes must retain the
                    # single-mutation latch instead of disappearing from
                    # proposal readiness.
                    proposal_blockers.append(
                        f"mutation_pending_{owner}_invalid:{command_id}"
                    )
                continue
            binding_blockers = _pending_suggestion_binding_blockers(
                record["command"],
                latest_state=latest_state,
                proposal_frame=proposal_frame,
                policy=policy,
            )
            if binding_blockers:
                stale_pending.append(
                    {
                        "command_id": command_id,
                        "action": record["action"],
                        "path": record["path"],
                        "blockers": binding_blockers,
                    }
                )
                if mutating:
                    proposal_blockers.extend(f"{item}:{command_id}" for item in binding_blockers)
            elif (
                mutating
                and mode == "suggested"
                and _pending_mutation_context_matches(
                    record["command"],
                    latest_state=latest_state,
                    proposal_frame=proposal_frame,
                    policy=policy,
                )
            ):
                # A pending mutation is already the selected intent for this
                # exact decision context.  Do not emit or plan another mutation
                # until it leaves pending.  Stale suggested bindings are handled
                # above (and rejected by ``expire_pending_commands`` before
                # synchronization), so they never masquerade as a current
                # selection.  One blocker per id also exposes legacy/malformed
                # multi-selection states without silently arbitrating them.
                proposal_blockers.append(f"mutation_pending_selection:{command_id}")
            elif mutating and mode == "suggested" and proposal_frame is not None:
                # The pre-frame scan deliberately cannot prove a frame-bound
                # candidate.  Once a frame is supplied, however, every
                # readable suggested mutation must either be classified stale,
                # become the exact current selection, or fail closed here.
                proposal_blockers.append(
                    f"mutation_pending_suggestion_invalid:{command_id}"
                )
            continue
        if not mutating:
            continue
        if record["stage"] == "approving":
            proposal_blockers.append(f"mutation_approval_claimed:{command_id}")
        elif record["stage"] == "approved":
            proposal_blockers.append(f"mutation_approved_in_flight:{command_id}")
        elif record["stage"] == "executing":
            proposal_blockers.append(f"mutation_execution_claimed:{command_id}")
        elif record["stage"] == "confirming":
            if classification == "confirmed_preparation":
                proposal_blockers.append(f"mutation_confirmation_commit_pending:{command_id}")
            elif classification == "awaiting_snapshot_confirmation":
                proposal_blockers.append(f"mutation_confirmation_in_flight:{command_id}")
            else:
                blocker = f"mutation_confirmation_ambiguous:{command_id}"
                proposal_blockers.append(blocker)
                confirmation_blockers.append(blocker)
                ambiguous_outcomes.append(
                    {
                        "command_id": command_id,
                        "classification": classification,
                        "stages": ["confirming"],
                        "mutating": True,
                    }
                )
        elif record["stage"] == "failed" and classification == "outcome_unknown":
            blocker = f"mutation_outcome_unknown_unresolved:{command_id}"
            proposal_blockers.append(blocker)
            confirmation_blockers.append(blocker)
            ambiguous_outcomes.append(
                {
                    "command_id": command_id,
                    "classification": "outcome_unknown",
                    "stages": ["failed"],
                    "mutating": True,
                }
            )

    public_records = [
        {key: value for key, value in record.items() if key not in {"command", "intent_hash"}}
        for record in sorted(records, key=_record_sort_key)
    ]
    proposal_blockers = sorted(set(proposal_blockers))
    confirmation_blockers = sorted(set(confirmation_blockers))
    confirmation_candidates = sorted(
        {
            str(record["command_id"])
            for record in records
            if record["stage"] == "confirming"
            and record["mutating"]
            and record["classification"] in {"awaiting_snapshot_confirmation", "confirmed_preparation"}
        }
    )
    duplicate_command_ids = {
        str(item["command_id"])
        for item in duplicate_ids
        if isinstance(item.get("command_id"), str)
    }
    pending_candidates: List[Dict[str, Any]] = []
    for record in sorted(records, key=_record_sort_key):
        if record["stage"] != "pending" or str(record["command_id"]) in duplicate_command_ids:
            continue
        if record["path"] != f"commands/pending/{record['command_id']}.json":
            continue
        candidate = _pending_mutation_candidate_summary(
            record["command"],
            latest_state=latest_state,
            proposal_frame=proposal_frame,
            policy=policy,
        )
        if candidate is not None:
            pending_candidates.append(candidate)
    pending_candidates.sort(key=lambda item: (str(item["command_id"]), str(item["intent_hash"])))
    return {
        "schema": COMMAND_PIPELINE_SCHEMA,
        "evaluation_context": evaluation_context,
        "ready": not proposal_blockers,
        "blockers": proposal_blockers,
        "readiness": {
            "mutation_proposal": {
                "ready": not proposal_blockers,
                "blockers": proposal_blockers,
            },
            "mutation_confirmation": {
                "ready": not confirmation_blockers,
                "blockers": confirmation_blockers,
                "candidate_ids": confirmation_candidates,
            },
        },
        "counts": counts,
        "commands": public_records,
        # This is the only command-shaped input exposed to ControlPacket.  It
        # contains no path, args, conditions, effects, reason, evidence, or raw
        # command body, and only includes a suggested mutation whose full
        # frame/state/policy binding is current.
        "pending_candidates": pending_candidates,
        "in_flight_mutations": [
            record
            for record in public_records
            if record["mutating"] and record["stage"] in {"approving", "approved", "executing", "confirming"}
        ],
        "ambiguous_outcomes": sorted(
            ambiguous_outcomes,
            key=lambda item: (str(item.get("command_id")), str(item.get("classification"))),
        ),
        "duplicate_ids": duplicate_ids,
        "stale_pending": sorted(stale_pending, key=lambda item: str(item.get("command_id"))),
        "invalid_records": invalid_records,
    }


def _record_sort_key(record: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (
        str(record.get("command_id") or ""),
        str(record.get("stage") or ""),
        str(record.get("path") or ""),
    )


def _pipeline_classification(stage: str, command: Mapping[str, Any], *, mutating: bool) -> str:
    if not mutating:
        return "non_mutating"
    if stage == "pending":
        return "pending_proposal"
    if stage == "approving":
        return "approval_claimed"
    if stage == "approved":
        return "approved_waiting_execution"
    if stage == "executing":
        return "execution_claimed"
    if stage == "failed":
        return "outcome_unknown"
    if command.get("status") == "executed_pending_snapshot":
        return "awaiting_snapshot_confirmation"
    if command.get("status") == "confirmed":
        confirmation = command.get("confirmation")
        if (
            isinstance(confirmation, Mapping)
            and confirmation.get("command_id") == command.get("id")
            and confirmation.get("confirmation_status") == "snapshot_confirmed"
        ):
            return "confirmed_preparation"
        return "ambiguous_confirmed_preparation"
    return "ambiguous_confirmation_state"


def _hash_envelope_is_valid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return value[7:] == value[7:].lower()


def _operator_pending_mutation_latches(command: Mapping[str, Any]) -> bool:
    """Recognize an approval-capable, unexpired operator mutation envelope.

    Current frame/policy matching is deliberately absent: operator and planned
    commands approve against their archived source frame, and a stale binding
    must remain visible for explicit operator disposition until its TTL expires.
    """

    action = command.get("action")
    source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
    expires_at = parse_timestamp(command.get("expires_at"))
    current = now()
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=current.tzinfo)
    autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
    route_valid = (
        command.get("schema") == COMMAND_SCHEMA
        and command.get("status") == "pending_human_approval"
    ) or (
        autonomous
        and command.get("status") == AUTONOMOUS_PENDING_STATUS
        and command.get("mode") == MODEL_SELECTED_MODE
        and all(autonomous_identifier_is_valid(command.get(key)) for key in ("run_id", "controller_id"))
    )
    return (
        route_valid
        and command.get("mode") in ARCHIVED_FRAME_PENDING_MODES
        and command.get("requires_approval") is True
        and isinstance(action, str)
        and action in ALLOWED_ACTIONS
        and action not in NON_MUTATING_ACTIONS
        and isinstance(command.get("args"), Mapping)
        and isinstance(command.get("session_id"), str)
        and bool(command.get("session_id"))
        and _hash_envelope_is_valid(command.get("save_id_hash"))
        and command.get("world_id") is not None
        and isinstance(command.get("source_snapshot_id"), str)
        and str(command.get("source_snapshot_id")).startswith(f"{command.get('session_id')}:")
        and _hash_envelope_is_valid(command.get("state_hash"))
        and _hash_envelope_is_valid(command.get("source_frame_id"))
        and _hash_envelope_is_valid(source.get("policy_hash"))
        and isinstance(command.get("preconditions"), list)
        and isinstance(command.get("expected_effects"), list)
        and expires_at is not None
        and expires_at > current
    )


def _failure_classifications(command: Mapping[str, Any]) -> set[str]:
    result = command.get("result") if isinstance(command.get("result"), Mapping) else {}
    return {
        value
        for value in (command.get("classification"), result.get("classification"))
        if isinstance(value, str) and value
    }


def has_operator_resolution(command: Mapping[str, Any]) -> bool:
    """Return whether an operator explicitly reconciled this failed command."""

    value = command.get("operator_resolution")
    reason = value.get("reason") if isinstance(value, Mapping) else None
    return (
        isinstance(value, Mapping)
        and value.get("schema") == OPERATOR_RESOLUTION_SCHEMA
        and value.get("command_id") == command.get("id")
        and value.get("resolved_by") == "local_human"
        and value.get("observed_outcome") in OPERATOR_RESOLUTION_OUTCOMES
        and isinstance(reason, str)
        and bool(reason.strip())
        and parse_timestamp(value.get("resolved_at")) is not None
    )


def _is_unresolved_outcome_unknown(command: Mapping[str, Any]) -> bool:
    """Return whether one failed record must latch further mutations.

    The latch is durable and global to this Bridge directory: it is cleared
    only by an explicit operator resolution, never by a later save (whose
    content hash always differs) or a reload.  A mutating failure latches
    unless the executor recorded a definite pre-dispatch classification;
    a missing classification (for example a raw executing claim moved to
    failed/ when finalization failed) cannot prove where dispatch stopped.
    """

    if has_operator_resolution(command):
        return False
    classifications = _failure_classifications(command)
    if "outcome_unknown" in classifications:
        return True
    if command.get("action") in NON_MUTATING_ACTIONS:
        return False
    return not classifications or not classifications <= _DEFINITE_FAILURE_CLASSIFICATIONS


def _command_intent_hash(command: Mapping[str, Any]) -> str:
    fields = _COMMAND_INTENT_FIELDS + (("run_id", "controller_id") if command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA else ())
    projection = {field: command.get(field) for field in fields}
    try:
        return canonical_document_hash(projection)
    except (CanonicalHashError, TypeError, ValueError, OverflowError):
        payload = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return "fallback-sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _policy_hash(policy: Optional[Mapping[str, Any]]) -> Optional[str]:
    return action_policy_hash(policy)


def _pending_mutation_context_matches(
    command: Mapping[str, Any],
    *,
    latest_state: Optional[Mapping[str, Any]],
    proposal_frame: Optional[Mapping[str, Any]],
    policy: Optional[Mapping[str, Any]],
) -> bool:
    """Return true only when all mutable proposal bindings are provably current.

    Bridge's first pipeline scan intentionally has no proposal frame yet.  That
    scan is projected with ``proposal_frame_evaluated`` false and must not make
    an otherwise-current suggested command stale by creating a self-referential
    blocked frame.  The frame-bound synchronization and Colony Plan checks call
    this gate again with the committed frame and enforce the selection blocker.
    """

    if not (
        isinstance(latest_state, Mapping)
        and isinstance(proposal_frame, Mapping)
        and isinstance(proposal_frame.get("frame_id"), str)
        and proposal_frame.get("frame_id")
        and isinstance(policy, Mapping)
    ):
        return False
    try:
        validate_decision_frame(proposal_frame)
    except (DecisionFrameError, TypeError, ValueError, OverflowError):
        return False
    policy_hash = _policy_hash(policy)
    frame_source = (
        proposal_frame.get("source")
        if isinstance(proposal_frame.get("source"), Mapping)
        else {}
    )
    frame_trust = (
        proposal_frame.get("trust")
        if isinstance(proposal_frame.get("trust"), Mapping)
        else {}
    )
    frame_policy_hash = safe_get(proposal_frame, "projections.policy.policy_hash")
    source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
    if command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA:
        if (
            autonomous_policy_state_blockers(policy, latest_state)
            or command.get("run_id") != policy.get("run_id")
            or command.get("controller_id") != policy.get("controller_id")
        ):
            return False
    elif policy.get("schema") != ACTION_POLICY_SCHEMA:
        return False
    if (
        policy_hash is None
        or source.get("policy_hash") != policy_hash
        or frame_policy_hash != policy_hash
        or frame_trust.get("status") != "trusted"
    ):
        return False
    state_hash = canonical_state_hash(dict(latest_state))
    return (
        frame_source.get("session_id") == latest_state.get("session_id")
        and frame_source.get("save_id_hash") == latest_state.get("save_id_hash")
        and frame_source.get("world_id") == latest_state.get("world_id")
        and frame_source.get("snapshot_id") == latest_state.get("snapshot_id")
        and frame_source.get("seq") == latest_state.get("seq")
        and frame_source.get("snapshot_seq") == latest_state.get("snapshot_seq")
        and frame_source.get("ts") == latest_state.get("ts")
        and frame_source.get("state_hash") == state_hash
        and command.get("session_id") == latest_state.get("session_id")
        and command.get("save_id_hash") == latest_state.get("save_id_hash")
        and command.get("world_id") == latest_state.get("world_id")
        and command.get("source_snapshot_id") == latest_state.get("snapshot_id")
        and command.get("state_hash") == state_hash
        and command.get("source_frame_id") == proposal_frame.get("frame_id")
    )


def _pending_mutation_candidate_summary(
    command: Mapping[str, Any],
    *,
    latest_state: Optional[Mapping[str, Any]],
    proposal_frame: Optional[Mapping[str, Any]],
    policy: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return a bounded non-executing summary for one exact current mutation."""

    action = command.get("action")
    risk = command.get("risk")
    command_id = command.get("id")
    source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
    created_at = parse_timestamp(command.get("created_at"))
    expires_at = parse_timestamp(command.get("expires_at"))
    current = now()
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=current.tzinfo)
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=current.tzinfo)
    intent_hash = _command_intent_hash(command)
    command_hint = {
        "action": action,
        "args": command.get("args"),
        "preconditions": command.get("preconditions"),
        "expected_effects": command.get("expected_effects"),
    }
    stable_id = stable_command_id(
        command.get("session_id"),
        str(command.get("source_incident_id") or ""),
        source.get("incident_opened_at"),
        str(action or ""),
        proposal={
            "args": command.get("args"),
            "preconditions": command.get("preconditions"),
            "expected_effects": command.get("expected_effects"),
        },
        source_frame_id=command.get("source_frame_id"),
        state_hash=command.get("state_hash"),
        source_policy_hash=source.get("policy_hash"),
    )
    frame_projections = (
        proposal_frame.get("projections")
        if isinstance(proposal_frame, Mapping)
        and isinstance(proposal_frame.get("projections"), Mapping)
        else {}
    )
    frame_advice = (
        frame_projections.get("advice")
        if isinstance(frame_projections.get("advice"), Mapping)
        else {}
    )
    projected_advice_items = (
        frame_advice.get("items") if isinstance(frame_advice.get("items"), list) else []
    )
    matching_projected_advice = [
        item
        for item in projected_advice_items
        if (
            isinstance(item, Mapping)
            and item.get("incident_id") == command.get("source_incident_id")
            and item.get("risk") == risk
            and item.get("incident_opened_at") == source.get("incident_opened_at")
        )
    ]
    advice_binding_matches = bool(
        frame_advice.get("valid") is True
        and len(matching_projected_advice) == 1
    )
    try:
        # Suggested commands are materialized only from the deterministic rules
        # advice path.  Re-evaluate that generator against the exact current
        # state and require the complete action hint to match, so a caller
        # cannot forge self-consistent args/conditions and merely recompute the
        # stable id.
        from agent import rules as agent_rules

        matching_rule_signals = [
            signal
            for signal in agent_rules.evaluate_signals(dict(latest_state or {}))
            if isinstance(signal, Mapping)
            and signal.get("incident_id") == command.get("source_incident_id")
            and isinstance(signal.get("command"), Mapping)
        ]
    except Exception:  # noqa: BLE001 - candidate generation must fail closed
        matching_rule_signals = []
    rules_command_matches = bool(
        len(matching_rule_signals) == 1
        and matching_rule_signals[0].get("state") == "active"
        and matching_rule_signals[0].get("risk") == risk
        and _strict_json_equal(matching_rule_signals[0]["command"], command_hint)
        and command.get("reason") == matching_rule_signals[0].get("reason")
        and _strict_json_equal(command.get("evidence"), matching_rule_signals[0].get("evidence"))
        and set(source)
        == {"kind", "title", "incident_opened_at", "state_seq", "state_ts", "policy_hash"}
        and source.get("kind") == "rule_incident"
        and source.get("title") == matching_rule_signals[0].get("title")
        and source.get("state_seq") == (latest_state or {}).get("seq")
        and source.get("state_ts") == (latest_state or {}).get("ts")
        and parse_timestamp(source.get("incident_opened_at")) is not None
    )
    arbitration_matches = True
    if action == "set_paused":
        frame_incidents = (
            frame_projections.get("incidents")
            if isinstance(frame_projections.get("incidents"), Mapping)
            else {}
        )
        projected_incidents = (
            frame_incidents.get("items")
            if isinstance(frame_incidents.get("items"), list)
            else []
        )
        incident_ids = [
            item.get("incident_id")
            for item in projected_incidents
            if isinstance(item, Mapping)
        ]
        advice_ids = [
            item.get("incident_id")
            for item in projected_advice_items
            if isinstance(item, Mapping)
        ]
        projection_complete = bool(
            frame_incidents.get("valid") is True
            and frame_incidents.get("truncated") is False
            and frame_incidents.get("count") == len(projected_incidents)
            and len(incident_ids) == len(set(incident_ids))
            and frame_advice.get("valid") is True
            and frame_advice.get("truncated") is False
            and frame_advice.get("count") == len(projected_advice_items)
            and len(advice_ids) == len(set(advice_ids))
        )
        open_incident_records = [
            dict(item)
            for item in projected_incidents
            if isinstance(item, Mapping) and item.get("status") == "open"
        ]
        try:
            arbitration = agent_rules.arbitrate_survival_emergency(
                latest_state,
                open_incident_records,
            )
        except Exception:  # noqa: BLE001 - candidate admission fails closed
            arbitration = {}
        expected_signal = (
            arbitration.get("expected_signal")
            if isinstance(arbitration.get("expected_signal"), Mapping)
            else {}
        )
        selected_witnesses = [
            item
            for item in open_incident_records
            if item.get("incident_id") == arbitration.get("selected_incident_id")
        ]
        arbitration_matches = bool(
            projection_complete
            and arbitration.get("status") == "selected"
            and arbitration.get("selected_incident_id")
            == command.get("source_incident_id")
            and _strict_json_equal(expected_signal.get("command"), command_hint)
            and expected_signal.get("risk") == risk
            and expected_signal.get("reason") == command.get("reason")
            and _strict_json_equal(expected_signal.get("evidence"), command.get("evidence"))
            and expected_signal.get("title") == source.get("title")
            and len(selected_witnesses) == 1
            and selected_witnesses[0].get("first_seen")
            == source.get("incident_opened_at")
            and not agent_rules.survival_suggested_command_frame_blockers(
                command,
                proposal_frame if isinstance(proposal_frame, Mapping) else {},
            )
        )
    policy_ttl_seconds = policy.get("command_ttl_seconds") if isinstance(policy, Mapping) else None
    policy_allows_candidate = bool(
        isinstance(policy, Mapping)
        and action_policy_is_valid(policy)
        and policy.get("actions_enabled") is True
        and policy.get("suggestions_enabled") is True
        and isinstance(policy.get("allowed_actions"), list)
        and action in policy.get("allowed_actions", [])
        and isinstance(policy.get("allowed_save_hashes"), list)
        and command.get("save_id_hash") in policy.get("allowed_save_hashes", [])
        and isinstance(policy_ttl_seconds, int)
        and not isinstance(policy_ttl_seconds, bool)
        and 1 <= policy_ttl_seconds <= 300
    )
    timing_binding_matches = bool(
        created_at is not None
        and expires_at is not None
        and isinstance(command.get("created_at"), str)
        and command.get("created_at") == created_at.isoformat(timespec="seconds")
        and isinstance(command.get("expires_at"), str)
        and command.get("expires_at") == expires_at.isoformat(timespec="seconds")
        and created_at <= current
        and expires_at > current
        and isinstance(policy_ttl_seconds, int)
        and not isinstance(policy_ttl_seconds, bool)
        and expires_at - created_at == dt.timedelta(seconds=policy_ttl_seconds)
    )
    autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
    intent_fields = set(_COMMAND_INTENT_FIELDS) | ({"run_id", "controller_id"} if autonomous else set())
    if not (
        set(command) == intent_fields | {"status"}
        and command.get("schema") in (COMMAND_SCHEMA, AUTONOMOUS_COMMAND_SCHEMA)
        and command.get("mode") == "suggested"
        and command.get("status") == (AUTONOMOUS_PENDING_STATUS if autonomous else "pending_human_approval")
        and command.get("requires_approval") is True
        and isinstance(command_id, str)
        and bool(command_id)
        and len(command_id) <= 160
        and isinstance(action, str)
        and action in ALLOWED_ACTIONS
        and action not in NON_MUTATING_ACTIONS
        and isinstance(command.get("args"), Mapping)
        and isinstance(command.get("preconditions"), list)
        and bool(command.get("preconditions"))
        and isinstance(command.get("expected_effects"), list)
        and bool(command.get("expected_effects"))
        and risk in {"high", "medium", "low", "unknown"}
        and timing_binding_matches
        and command_id == stable_id
        and advice_binding_matches
        and rules_command_matches
        and arbitration_matches
        and policy_allows_candidate
        and isinstance(latest_state, Mapping)
        and _advice_command_is_current_and_valid(
            dict(latest_state),
            {
                "source_snapshot_id": command.get("source_snapshot_id"),
                "command": command_hint,
            },
        )
        and _hash_envelope_is_valid(intent_hash)
        and _pending_mutation_context_matches(
            command,
            latest_state=latest_state,
            proposal_frame=proposal_frame,
            policy=policy,
        )
    ):
        return None
    return {
        "command_id": command_id,
        "action": action,
        "intent_hash": intent_hash,
        "risk": risk,
        "requires_approval": True,
        "auto_execute": False,
    }


def _pending_suggestion_binding_blockers(
    command: Mapping[str, Any],
    *,
    latest_state: Optional[Mapping[str, Any]],
    proposal_frame: Optional[Mapping[str, Any]],
    policy: Optional[Mapping[str, Any]],
) -> List[str]:
    if command.get("mode") != "suggested":
        return []
    if not (
        isinstance(latest_state, Mapping)
        and _trusted_suggestion_state(dict(latest_state))
    ):
        return []
    blockers: List[str] = []
    for field in ("session_id", "save_id_hash", "world_id"):
        if command.get(field) != latest_state.get(field):
            blockers.append(f"pending_source_{field}_stale")
    if command.get("source_snapshot_id") != latest_state.get("snapshot_id"):
        blockers.append("pending_source_snapshot_stale")
    if command.get("state_hash") != canonical_state_hash(dict(latest_state)):
        blockers.append("pending_source_state_hash_stale")
    if isinstance(proposal_frame, Mapping) and isinstance(proposal_frame.get("frame_id"), str):
        if command.get("source_frame_id") != proposal_frame.get("frame_id"):
            blockers.append("pending_source_frame_stale")
    current_policy_hash = _policy_hash(policy)
    if current_policy_hash is not None:
        source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
        bound_policy_hash = source.get("policy_hash")
        if not isinstance(bound_policy_hash, str):
            blockers.append("pending_source_policy_unbound")
        elif bound_policy_hash != current_policy_hash:
            blockers.append("pending_source_policy_stale")
    return list(dict.fromkeys(blockers))


def sync_pending_commands(
    bridge_dir: pathlib.Path,
    state: Optional[Dict[str, Any]],
    advice: Iterable[Dict[str, Any]],
    max_new_commands: int = 5,
    proposal_frame: Optional[Mapping[str, Any]] = None,
    allow_non_actionable_replay: bool = False,
    *,
    policy_override: Optional[Mapping[str, Any]] = None,
    before_publish: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    bridge_dir = bridge_dir.expanduser().resolve()
    with lifecycle_custody(bridge_dir):
        recovery = recover_lifecycle_transactions(bridge_dir)
        if int(recovery.get("unknown") or 0) > 0:
            raise CommandCustodyError(
                "unresolved command lifecycle recovery evidence blocks pending synchronization"
            )
        return _sync_pending_commands_locked(
            bridge_dir,
            state,
            advice,
            max_new_commands=max_new_commands,
            proposal_frame=proposal_frame,
            allow_non_actionable_replay=allow_non_actionable_replay,
            policy_override=policy_override,
            before_publish=before_publish,
        )


def _sync_pending_commands_locked(
    bridge_dir: pathlib.Path,
    state: Optional[Dict[str, Any]],
    advice: Iterable[Dict[str, Any]],
    max_new_commands: int = 5,
    proposal_frame: Optional[Mapping[str, Any]] = None,
    allow_non_actionable_replay: bool = False,
    *,
    policy_override: Optional[Mapping[str, Any]] = None,
    before_publish: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    dirs = ensure_command_dirs(bridge_dir)
    advice_list = list(advice)
    state_trusted = _trusted_suggestion_state(state)
    # Load one policy snapshot before any authorization decision.  A valid
    # historical frame must never authorize a command under a different current
    # policy merely because the state identity still matches.
    current_policy = load_policy(bridge_dir)
    if (
        isinstance(policy_override, Mapping)
        and _policy_hash(policy_override) is not None
        and _policy_hash(policy_override) == _policy_hash(current_policy)
    ):
        policy = dict(policy_override)
    else:
        policy = current_policy
    autonomous = policy.get("schema") == AUTONOMOUS_ACTION_POLICY_SCHEMA
    if autonomous and (
        policy.get("actions_enabled") is not True
        or autonomous_policy_state_blockers(policy, state)
    ):
        return []
    frame_authorized = _proposal_frame_authorizes(
        state,
        proposal_frame,
        allow_non_actionable_replay=allow_non_actionable_replay,
    )
    current_policy_hash = _policy_hash(policy)
    mutation_frame_policy_authorized = bool(
        not allow_non_actionable_replay
        and (
            current_policy_hash is not None
            and safe_get(proposal_frame, "projections.policy.valid", False) is True
            and safe_get(proposal_frame, "projections.policy.policy_hash")
            == current_policy_hash
        )
    )
    eligible_advice = [
        item
        for item in advice_list
        if state_trusted
        and frame_authorized
        and _advice_command_is_current_and_valid(state, item)
    ]
    open_incident_ids = {
        str(item.get("incident_id"))
        for item in eligible_advice
        if item.get("incident_id")
    }
    # Bridge passes the exact policy already projected into the committed
    # decision frame.  Other callers retain the historical load-on-call
    # behavior by omitting this keyword-only override.
    if policy.get("suggestions_enabled", True) is False:
        for path in sorted(dirs["pending"].glob("*.json")):
            try:
                raw, _identity_value, _mtime = read_stable_bytes(
                    path,
                    label=f"commands/pending/{path.name}",
                )
                command = json.loads(
                    raw.decode("utf-8-sig"),
                    parse_constant=_reject_json_constant,
                )
            except (CommandCustodyError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(command, dict):
                continue
            mode = str(command.get("mode") or "")
            if mode in {"suggested", MODEL_SELECTED_MODE}:
                # Both are Bridge-originated proposals; disabling suggestions
                # withdraws them (operator/planned commands stay).
                move_to_rejected(path, dirs["rejected"], command, "suggestions_disabled")
            elif mode not in OPERATOR_PENDING_MODES:
                action = str(command.get("action") or "")
                if action in NON_MUTATING_ACTIONS:
                    move_to_rejected(path, dirs["rejected"], command, "unsupported_pending_mode")
        expire_pending_commands(
            dirs,
            open_incident_ids,
            latest_state=state if state_trusted else None,
            proposal_frame=proposal_frame,
            policy=policy,
        )
        return []

    expire_pending_commands(
        dirs,
        open_incident_ids,
        latest_state=state if state_trusted else None,
        proposal_frame=proposal_frame,
        policy=policy,
    )
    if not state_trusted or not frame_authorized:
        return []
    pipeline = command_pipeline_readiness(
        bridge_dir,
        latest_state=state,
        proposal_frame=proposal_frame,
        policy=policy,
    )
    mutation_ready = bool(
        safe_get(pipeline, "readiness.mutation_proposal.ready", False)
    )
    configured_limit = _positive_int(policy.get("max_pending_commands"), DEFAULT_MAX_PENDING)
    pending_count = len(list(dirs["pending"].glob("*.json")))
    available_slots = max(0, configured_limit - pending_count)
    creation_limit = min(max_new_commands, available_slots)

    existing = existing_command_ids(dirs.values())
    created: List[Dict[str, Any]] = []
    mutating_created = False
    ttl_seconds = _positive_int(policy.get("command_ttl_seconds"), DEFAULT_TTL_SECONDS)
    current = now()
    resuggest_window = dt.timedelta(seconds=NON_MUTATING_RESUGGEST_SECONDS)
    recent_stale: Optional[Dict[str, dt.datetime]] = None

    for item in eligible_advice:
        if len(created) >= creation_limit:
            break
        command_hint = item.get("command")
        if not isinstance(command_hint, dict):
            continue
        action = str(command_hint.get("action") or "")
        if action not in ALLOWED_ACTIONS:
            continue
        mutating = action not in NON_MUTATING_ACTIONS
        if autonomous and not mutating:
            # Informational advice remains visible without occupying the sole
            # autonomous mutation slot or inventing a receipt-free V4 route.
            continue
        if autonomous and action not in policy.get("allowed_actions", []):
            continue
        if mutating:
            if (
                allow_non_actionable_replay
                or not mutation_frame_policy_authorized
                or not mutation_ready
                or mutating_created
            ):
                continue
        else:
            if recent_stale is None:
                recent_stale = _recent_source_stale_rejections(
                    dirs["rejected"], since=current - resuggest_window
                )
            last_stale = recent_stale.get(str(item.get("incident_id") or ""))
            if last_stale is not None and current - last_stale < resuggest_window:
                continue

        command = build_command(
            state,
            item,
            command_hint,
            ttl_seconds=ttl_seconds,
            source_frame_id=str(proposal_frame.get("frame_id")),
            source_policy_hash=_policy_hash(policy),
            policy=policy if autonomous else None,
        )
        if command["id"] in existing:
            continue
        if autonomous and before_publish is not None:
            before_publish(copy.deepcopy(command))
        publish_pending_command(bridge_dir, command)
        existing.add(command["id"])
        created.append(command)
        if mutating:
            mutating_created = True

    return created


def _recent_source_stale_rejections(
    rejected_dir: pathlib.Path,
    *,
    since: dt.datetime,
) -> Dict[str, dt.datetime]:
    """Latest source-stale rejection per incident among recent non-mutating suggestions.

    Only files written since ``since`` are read (rejected/ grows for the whole
    session); a torn or malformed record simply does not throttle.
    """

    latest: Dict[str, dt.datetime] = {}
    # File mtimes are set by the rejection transition; a small slack covers
    # clock granularity between the transition and ``rejected_at``.
    threshold = since.timestamp() - 5.0
    try:
        entries = list(os.scandir(rejected_dir))
    except OSError:
        return latest
    for entry in entries:
        try:
            if (
                not entry.name.endswith(".json")
                or not entry.is_file()
                or entry.stat().st_mtime < threshold
            ):
                continue
            payload = json.loads(
                pathlib.Path(entry.path).read_text(encoding="utf-8-sig"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("mode") != "suggested"
            or str(payload.get("action") or "") not in NON_MUTATING_ACTIONS
            or payload.get("rejection_reason") not in SOURCE_STALE_REJECTION_REASONS
        ):
            continue
        incident_id = str(payload.get("source_incident_id") or "")
        rejected_at = parse_timestamp(payload.get("rejected_at"))
        if not incident_id or rejected_at is None:
            continue
        if rejected_at.tzinfo is None:
            rejected_at = rejected_at.replace(tzinfo=since.tzinfo)
        if incident_id not in latest or rejected_at > latest[incident_id]:
            latest[incident_id] = rejected_at
    return latest


def trusted_proposal_state(state: Optional[Dict[str, Any]]) -> bool:
    """Public form of the trusted-snapshot gate applied before any proposal."""

    return _trusted_suggestion_state(state)


def command_hint_is_current(state: Optional[Dict[str, Any]], hint: Any) -> bool:
    """Whether a catalogued command hint is valid and its preconditions hold now."""

    if not isinstance(state, dict):
        return False
    return _advice_command_is_current_and_valid(
        state,
        {"source_snapshot_id": state.get("snapshot_id"), "command": hint},
    )


def _trusted_suggestion_state(state: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("schema") != "oni.state.v2" or state.get("event") != "state_snapshot":
        return False
    bridge = state.get("bridge")
    if not isinstance(bridge, dict):
        return False
    blockers = bridge.get("trust_blockers")
    return (
        bridge.get("trust_ready") is True
        and bridge.get("stale") is False
        and isinstance(blockers, list)
        and not blockers
        and all(isinstance(item, str) for item in blockers)
    )


def _proposal_frame_authorizes(
    state: Optional[Dict[str, Any]],
    frame: Optional[Mapping[str, Any]],
    *,
    allow_non_actionable_replay: bool = False,
) -> bool:
    if not isinstance(state, dict) or not isinstance(frame, Mapping):
        return False
    try:
        validate_decision_frame(frame)
    except (DecisionFrameError, TypeError, ValueError, OverflowError):
        return False
    source = frame.get("source") if isinstance(frame.get("source"), Mapping) else {}
    readiness = frame.get("readiness") if isinstance(frame.get("readiness"), Mapping) else {}
    propose = readiness.get("propose") if isinstance(readiness.get("propose"), Mapping) else {}
    trust = frame.get("trust") if isinstance(frame.get("trust"), Mapping) else {}
    projections = frame.get("projections") if isinstance(frame.get("projections"), Mapping) else {}
    health_projection = (
        projections.get("health") if isinstance(projections.get("health"), Mapping) else {}
    )
    live_authorized = (
        trust.get("status") == "trusted"
        and propose.get("ready") is True
        and not list(propose.get("blockers") or [])
    )
    isolated_replay_authorized = (
        allow_non_actionable_replay
        and health_projection.get("replay_mode") is True
        and trust.get("status") in {"trusted", "blocked"}
        and "REPLAY_MODE" in list(propose.get("blockers") or [])
    )
    return (
        (live_authorized or isolated_replay_authorized)
        and source.get("session_id") == state.get("session_id")
        and source.get("save_id_hash") == state.get("save_id_hash")
        and source.get("world_id") == state.get("world_id")
        and source.get("snapshot_id") == state.get("snapshot_id")
        and source.get("seq") == state.get("seq")
        and source.get("snapshot_seq") == state.get("snapshot_seq")
        and source.get("ts") == state.get("ts")
        and source.get("state_hash") == canonical_state_hash(state)
    )


def _advice_command_is_current_and_valid(
    state: Optional[Dict[str, Any]],
    item: Any,
) -> bool:
    if not isinstance(state, dict) or not isinstance(item, dict):
        return False
    if item.get("source_snapshot_id") != state.get("snapshot_id"):
        return False
    command_hint = item.get("command")
    if not isinstance(command_hint, dict):
        return False
    action = command_hint.get("action")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        return False
    args = command_hint.get("args")
    if not isinstance(args, dict):
        return False
    preconditions = command_hint.get("preconditions")
    effects = command_hint.get("expected_effects")
    if not isinstance(preconditions, list) or not isinstance(effects, list):
        return False
    if action not in NON_MUTATING_ACTIONS and (not preconditions or not effects):
        return False
    for condition in preconditions:
        if condition_contract_blockers(state, condition, action=str(action)):
            return False
        if not _condition_matches(state, condition):
            return False
    for condition in effects:
        # Effects describe the post-action snapshot, so newly-created dynamic paths
        # may not exist yet. Validate their trusted declaration now; the executor
        # validates the resulting field and exact quality metadata after mutation.
        if condition_declaration_blockers(condition, action=str(action)):
            return False
    return True


def _condition_matches(state: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
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
    left = float(actual)
    right = float(expected)
    if not math.isfinite(left) or not math.isfinite(right):
        return False
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    return left >= right


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


def build_command(
    state: Optional[Dict[str, Any]],
    advice_item: Dict[str, Any],
    command_hint: Dict[str, Any],
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    source_frame_id: Optional[str] = None,
    source_policy_hash: Optional[str] = None,
    policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    snapshot = state if isinstance(state, dict) else {}
    action = str(command_hint["action"])
    args = command_hint.get("args") if isinstance(command_hint.get("args"), dict) else {}
    incident_id = str(advice_item.get("incident_id") or "")
    opened_at = advice_item.get("incident_opened_at")
    session_id = snapshot.get("session_id")
    state_hash = canonical_state_hash(snapshot)
    command_id = stable_command_id(
        session_id,
        incident_id,
        opened_at,
        action,
        proposal={
            "args": args,
            "preconditions": list(command_hint.get("preconditions") or []),
            "expected_effects": list(command_hint.get("expected_effects") or []),
        },
        source_frame_id=source_frame_id,
        state_hash=state_hash,
        source_policy_hash=source_policy_hash,
    )
    created = now()
    expires = created + dt.timedelta(seconds=max(1, ttl_seconds))

    source = {
        "kind": "rule_incident",
        "title": advice_item.get("title"),
        "incident_opened_at": opened_at,
        "state_seq": snapshot.get("seq"),
        "state_ts": snapshot.get("ts"),
    }
    if isinstance(source_policy_hash, str) and source_policy_hash:
        source["policy_hash"] = source_policy_hash

    autonomous = isinstance(policy, Mapping) and policy.get("schema") == AUTONOMOUS_ACTION_POLICY_SCHEMA
    if autonomous:
        if (
            autonomous_policy_state_blockers(policy, snapshot)
            or policy.get("actions_enabled") is not True
            or policy.get("suggestions_enabled") is not True
            or action not in policy.get("allowed_actions", [])
            or source_policy_hash != action_policy_hash(policy)
            or ttl_seconds != policy.get("command_ttl_seconds")
        ):
            raise CommandCustodyConflict("autonomous suggested command policy/state binding is invalid")
    command = {
        "schema": AUTONOMOUS_COMMAND_SCHEMA if autonomous else COMMAND_SCHEMA,
        "id": command_id,
        "mode": "suggested",
        "status": AUTONOMOUS_PENDING_STATUS if autonomous else "pending_human_approval",
        "action": action,
        "args": args,
        "session_id": session_id,
        "save_id_hash": snapshot.get("save_id_hash"),
        "world_id": snapshot.get("world_id"),
        "source_incident_id": incident_id,
        "source_snapshot_id": snapshot.get("snapshot_id"),
        "state_hash": state_hash,
        "source_frame_id": source_frame_id,
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "risk": advice_item.get("risk"),
        "reason": advice_item.get("reason"),
        "evidence": advice_item.get("evidence"),
        "preconditions": list(command_hint.get("preconditions") or []),
        "expected_effects": list(command_hint.get("expected_effects") or []),
        "requires_approval": True,
        "source": source,
    }
    if autonomous:
        command["run_id"] = policy.get("run_id")
        command["controller_id"] = policy.get("controller_id")
    return command


def stable_command_id(
    session_id: Any,
    incident_id: str,
    opened_at: Any,
    action: str,
    proposal: Optional[Mapping[str, Any]] = None,
    *,
    source_frame_id: Any = None,
    state_hash: Any = None,
    source_policy_hash: Any = None,
) -> str:
    """Return an idempotency key for one exact suggested decision context.

    The immutable incident episode and proposal identify the logical intent;
    the frame/state/policy bindings distinguish a newly observed authorization
    context after an unapproved stale suggestion is rejected.  This preserves
    every old audit artifact without suppressing a fresh re-derivation, while
    repeated rendering of the same exact context remains idempotent.
    """

    payload = json.dumps(
        {
            "session_id": session_id,
            "incident_id": incident_id,
            "opened_at": opened_at,
            "action": action,
            "proposal": dict(proposal) if isinstance(proposal, Mapping) else {},
            "source_frame_id": source_frame_id,
            "state_hash": state_hash,
            "source_policy_hash": source_policy_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # 128 bits keeps filenames compact while avoiding a 64-bit collision
    # boundary during indefinite operation and long-lived audit retention.
    return "cmd-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def expire_pending_commands(
    dirs: Dict[str, pathlib.Path],
    open_incident_ids: Set[str],
    *,
    latest_state: Optional[Mapping[str, Any]] = None,
    proposal_frame: Optional[Mapping[str, Any]] = None,
    policy: Optional[Mapping[str, Any]] = None,
) -> None:
    bridge_dir = dirs["pending"].parent.parent
    with lifecycle_custody(bridge_dir):
        recovery = recover_lifecycle_transactions(bridge_dir)
        if int(recovery.get("unknown") or 0) > 0:
            raise CommandCustodyError(
                "unresolved command lifecycle recovery evidence blocks pending expiry"
            )
        _expire_pending_commands_locked(
            dirs,
            open_incident_ids,
            latest_state=latest_state,
            proposal_frame=proposal_frame,
            policy=policy,
        )


def _expire_pending_commands_locked(
    dirs: Dict[str, pathlib.Path],
    open_incident_ids: Set[str],
    *,
    latest_state: Optional[Mapping[str, Any]] = None,
    proposal_frame: Optional[Mapping[str, Any]] = None,
    policy: Optional[Mapping[str, Any]] = None,
) -> None:
    current = now()
    for path in sorted(dirs["pending"].glob("*.json")):
        try:
            raw, _identity_value, _mtime = read_stable_bytes(
                path,
                label=f"commands/pending/{path.name}",
            )
            command = json.loads(
                raw.decode("utf-8-sig"),
                parse_constant=_reject_json_constant,
            )
        except (CommandCustodyError, UnicodeError, json.JSONDecodeError, ValueError):
            # Malformed pending envelopes are retained for explicit operator
            # inspection. They are not safe to destroy or turn into a guessed
            # terminal record.
            continue
        if not isinstance(command, dict):
            continue
        mode = command.get("mode")
        action = str(command.get("action") or "")
        if (
            action not in NON_MUTATING_ACTIONS
            and mode not in ARCHIVED_FRAME_PENDING_MODES
            and mode != "suggested"
        ):
            # Ownership is ambiguous.  Preserve the exact readable envelope and
            # let command_pipeline_readiness expose its fail-closed blocker.
            continue
        incident_id = str(command.get("source_incident_id") or "")
        expires_at = parse_timestamp(command.get("expires_at"))
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=current.tzinfo)
        reason: Optional[str] = None
        binding_blockers = _pending_suggestion_binding_blockers(
            command,
            latest_state=latest_state,
            proposal_frame=proposal_frame,
            policy=policy,
        )
        if mode == "suggested" and incident_id and incident_id not in open_incident_ids:
            reason = "source_incident_not_open"
        elif binding_blockers:
            reason = binding_blockers[0]
        elif expires_at is None or expires_at <= current:
            reason = "expired_before_approval"
        if reason:
            move_to_rejected(path, dirs["rejected"], command, reason)

    # An approved command the executor never claimed expires with no disposition:
    # the autonomous single-in-flight gate counts it as an active intent forever,
    # so the loop can neither publish a new command nor resume the game that the
    # command needed (2026-10-26 rounds 4, 34).  A command still in ``approved``
    # with no dispatch evidence was never executed, so rejecting it dispatches
    # nothing; any dispatch evidence means the outcome may be uncertain and is
    # left for explicit reconciliation.
    for path in sorted(dirs["approved"].glob("*.json")):
        try:
            raw, _identity_value, _mtime = read_stable_bytes(
                path,
                label=f"commands/approved/{path.name}",
            )
            command = json.loads(
                raw.decode("utf-8-sig"),
                parse_constant=_reject_json_constant,
            )
        except (CommandCustodyError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(command, dict):
            continue
        if any(command.get(field) for field in ("attempt_id", "execution_started_at", "executed_ack_seq")):
            continue
        expires_at = parse_timestamp(command.get("expires_at"))
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=current.tzinfo)
        if expires_at is None or expires_at > current:
            continue
        move_to_rejected(
            path,
            dirs["rejected"],
            command,
            "expired_before_dispatch",
            source_stage="approved",
        )


def move_to_rejected(
    source: pathlib.Path,
    rejected_dir: pathlib.Path,
    command: Optional[Dict[str, Any]],
    reason: str,
    *,
    source_stage: str = "pending",
) -> Optional[pathlib.Path]:
    bridge_dir = source.parent.parent.parent
    with lifecycle_custody(bridge_dir):
        recovery = recover_lifecycle_transactions(bridge_dir)
        if int(recovery.get("unknown") or 0) > 0:
            raise CommandCustodyError(
                "unresolved command lifecycle recovery evidence blocks rejection"
            )
        try:
            raw, _identity_value, _mtime = read_stable_bytes(
                source,
                label=f"{source_stage} source",
            )
            actual = json.loads(
                raw.decode("utf-8-sig"),
                parse_constant=_reject_json_constant,
            )
        except (CommandCustodyError, UnicodeError, json.JSONDecodeError, ValueError):
            # A malformed or replaced pending record remains in place.
            return None
        if not isinstance(actual, dict):
            return None
        if command is not None and _command_intent_hash(command) != _command_intent_hash(actual):
            raise CommandCustodyConflict("pending rejection envelope changed before custody claim")
        payload = dict(actual)
        payload["id"] = payload.get("id") or source.stem
        if payload["id"] != source.stem:
            raise CommandCustodyConflict("pending rejection id does not match its canonical filename")
        payload.update(
            {
                "schema": payload.get("schema") or COMMAND_SCHEMA,
                "status": "rejected",
                "rejection_reason": reason,
                "rejected_at": now_iso(),
            }
        )
        target = rejected_dir / source.name
        return transition_pending_envelope(
            bridge_dir,
            source,
            target,
            payload,
            operation="rejected",
            source_stage=source_stage,
        )


def unique_target(directory: pathlib.Path, stem: str) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{stem}.json"
    if not target.exists():
        return target
    suffix = now().strftime("%Y%m%d%H%M%S")
    return directory / f"{stem}-{suffix}.json"


def existing_command_ids(paths: Iterable[pathlib.Path]) -> Set[str]:
    ids: Set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for item in path.glob("*.json"):
            ids.add(item.stem.split("-", 3)[0] if item.stem.startswith("cmd-") and item.stem.count("-") > 2 else item.stem)
            try:
                value = json.loads(item.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            command_id = value.get("id") if isinstance(value, dict) else None
            if command_id:
                ids.add(str(command_id))
    return ids


def write_json_atomic(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[pathlib.Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp"
        ) as handle:
            temporary = pathlib.Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        _replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _replace_with_retry(temporary: pathlib.Path, path: pathlib.Path) -> None:
    """Replace ``path`` while tolerating short-lived Windows sharing locks."""

    delay = _ATOMIC_REPLACE_INITIAL_DELAY_SECONDS
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            temporary.replace(path)
            return
        except OSError as exc:
            if attempt + 1 >= _ATOMIC_REPLACE_ATTEMPTS or not _is_retryable_replace_error(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, _ATOMIC_REPLACE_MAX_DELAY_SECONDS)


def _is_retryable_replace_error(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or exc.errno in _RETRYABLE_REPLACE_ERRNOS
        or getattr(exc, "winerror", None) in _RETRYABLE_REPLACE_WINERRORS
    )


def _positive_int(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback
