"""Explicit autonomous authority through the existing durable approval transaction.

This module performs no model or network calls. A refusal is not execution
evidence, and an uncertain transaction must be reconciled under its original id.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from agent import command_custody
from agent.action_outcomes import command_intent_hash, command_intent_projection
from agent.action_policy import AUTONOMOUS_ACTION_POLICY_SCHEMA, autonomous_policy_state_blockers
from agent.approval_receipt import (
    ApprovalReceiptError,
    build_approved_command,
    validate_approval_inputs,
)
from agent.canonical_hash import CanonicalHashError, canonical_bytes, loads_strict
from agent.command_queue import (
    AUTONOMOUS_COMMAND_SCHEMA,
    AUTONOMOUS_PENDING_STATUS,
    _pending_mutation_candidate_summary,
    command_hint_is_current,
    command_pipeline_readiness,
    trusted_proposal_state,
)
from agent.decision_frame_archive import DecisionFrameArchiveError, decision_frame_archive_path, load_archived_decision_frame
from agent.field_contracts import NON_MUTATING_ACTIONS
from agent.state_quality import canonical_state_hash, parse_snapshot_timestamp


AUTONOMOUS_CONTROL_SCHEMA = "oni.autonomous-control.v1"
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_KEYS = frozenset(
    {"kind", "candidate_id", "candidate_set_hash", "request_id", "state_seq", "state_ts", "policy_hash"}
)
_EVIDENCE_KEYS = frozenset(
    {"candidate_id", "candidate_set_hash", "model_reason", "model", "prompt_hash", "response_hash", "request_id"}
)
_STARTUP_KEYS = frozenset(
    {
        "validation_run_id", "validation_process_id", "startup_binding_hash",
        "startup_intent_hash", "runtime_config_evidence_sha256", "loader_path_id_upper",
        "loader_save_sha256", "game_build", "mod_version",
    }
)
_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


class AutonomousAuthorizationError(RuntimeError):
    """A known refusal; no authority or execution is claimed."""

    outcome_unknown = False


class AutonomousAuthorizationOutcomeUnknown(AutonomousAuthorizationError):
    """Keep the existing intent/id and observe custody; never resend."""

    outcome_unknown = True


@dataclass(frozen=True)
class AutonomousAuthorizationContext:
    bridge_dir: pathlib.Path
    state: Dict[str, Any]
    frame: Dict[str, Any]
    policy: Dict[str, Any]
    control: Dict[str, Any]
    archive_path: pathlib.Path
    files: Dict[pathlib.Path, Tuple[bytes, Tuple[int, int], int]]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AutonomousAuthorizationError(message)


def _equal(left: Any, right: Any) -> bool:
    try:
        return canonical_bytes(left) == canonical_bytes(right)
    except (CanonicalHashError, TypeError, ValueError, OverflowError):
        return False


def _read_object(path: pathlib.Path) -> Tuple[Dict[str, Any], Tuple[bytes, Tuple[int, int], int]]:
    raw, identity, mtime = command_custody.read_stable_bytes(
        path, label=f"autonomous input {path.name}", max_bytes=_MAX_DOCUMENT_BYTES,
    )
    try:
        value = loads_strict(raw.decode("utf-8-sig"))
    except (UnicodeError, CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise AutonomousAuthorizationError(f"autonomous input is not strict JSON: {path.name}") from exc
    _require(isinstance(value, dict), f"autonomous input is not an object: {path.name}")
    return value, (raw, identity, mtime)


def validate_autonomous_control(
    control: Mapping[str, Any], policy: Mapping[str, Any], state: Mapping[str, Any],
) -> None:
    """Require a running controller and exact external startup anchors."""

    required = {
        "schema", "run_id", "controller_id", "session_id", "world_id",
        "save_id_hash", "startup_expectations", "desired_state",
    }
    _require(required <= set(control) <= required | {"updated_at", "controller_pid"}, "autonomous control shape is invalid")
    _require(control.get("schema") == AUTONOMOUS_CONTROL_SCHEMA, "autonomous control schema is invalid")
    _require(control.get("desired_state") == "running", "autonomous authority is paused or stopped")
    for key in ("run_id", "controller_id", "session_id", "world_id"):
        _require(_equal(control.get(key), policy.get(key)), f"autonomous control {key} mismatch")
    _require(control.get("save_id_hash") == state.get("save_id_hash"), "autonomous control save mismatch")
    if "updated_at" in control:
        _require(parse_snapshot_timestamp(control.get("updated_at")) is not None, "autonomous control timestamp is invalid")
    if "controller_pid" in control:
        _require(type(control.get("controller_pid")) is int and control["controller_pid"] > 0, "autonomous controller pid is invalid")
    expectations = control.get("startup_expectations")
    _require(isinstance(expectations, Mapping) and set(expectations) == _STARTUP_KEYS, "autonomous startup expectations are incomplete")
    _require(expectations.get("validation_run_id") == policy.get("run_id"), "autonomous startup run mismatch")
    _require(type(expectations.get("validation_process_id")) is int and expectations["validation_process_id"] > 0, "autonomous startup process id is invalid")
    for key in ("startup_binding_hash", "startup_intent_hash", "runtime_config_evidence_sha256", "loader_path_id_upper", "loader_save_sha256"):
        _require(isinstance(expectations.get(key), str) and _HASH_RE.fullmatch(expectations[key]) is not None, f"autonomous startup {key} is invalid")
    for key in ("game_build", "mod_version"):
        _require(isinstance(expectations.get(key), str) and bool(expectations[key].strip()) and len(expectations[key]) <= 80, f"autonomous startup {key} is invalid")
    for key in _STARTUP_KEYS - {"loader_path_id_upper", "loader_save_sha256"}:
        _require(_equal(expectations.get(key), state.get(key)), f"autonomous observed startup {key} mismatch")
    _require(state.get("requested_save_path_id") == expectations["loader_path_id_upper"], "autonomous requested clone path mismatch")
    _require(state.get("active_save_path_id") == expectations["loader_path_id_upper"], "autonomous active clone path mismatch")
    _require(state.get("save_id_hash") == expectations["loader_save_sha256"], "autonomous loaded clone content mismatch")


def _validate_provenance(
    command: Mapping[str, Any], policy: Mapping[str, Any],
    frame: Mapping[str, Any], state: Mapping[str, Any],
) -> None:
    if command.get("mode") == "suggested":
        candidate = _pending_mutation_candidate_summary(
            command, latest_state=state, proposal_frame=frame, policy=policy,
        )
        _require(candidate is not None, "autonomous suggested rule/certificate binding is invalid")
        return
    _require(command.get("mode") == "model_selected", "autonomous mutation mode is unsupported")
    from agent.candidates import generate_candidates

    source = command.get("source")
    evidence = command.get("evidence")
    _require(isinstance(source, Mapping) and set(source) == _SOURCE_KEYS, "autonomous model source shape is invalid")
    _require(isinstance(evidence, Mapping) and set(evidence) == _EVIDENCE_KEYS, "autonomous model evidence shape is invalid")
    _require(source.get("kind") == "model_choice", "autonomous model source provenance is invalid")
    for key in ("candidate_id", "candidate_set_hash", "request_id"):
        _require(source.get(key) == evidence.get(key), f"autonomous model {key} evidence mismatch")
    _require(isinstance(source.get("request_id"), str) and 0 < len(source["request_id"]) <= 160, "autonomous model request id is invalid")
    for key in ("candidate_set_hash", "prompt_hash", "response_hash"):
        _require(isinstance(evidence.get(key), str) and _HASH_RE.fullmatch(evidence[key]) is not None, f"autonomous model {key} is invalid")
    _require(isinstance(evidence.get("model"), str) and 0 < len(evidence["model"]) <= 160, "autonomous model identity is invalid")
    _require(isinstance(evidence.get("model_reason"), str) and len(evidence["model_reason"]) <= 500, "autonomous model reason is invalid")
    _require(_equal(source.get("state_seq"), state.get("seq")) and source.get("state_ts") == state.get("ts"), "autonomous model source freshness mismatch")
    _require(command.get("source_incident_id") is None, "autonomous model command has a rule incident identity")
    catalog = generate_candidates(state, allowed_actions=policy["allowed_actions"], autonomous=True)
    _require(catalog.get("candidate_set_hash") == source.get("candidate_set_hash"), "autonomous model candidate set is stale")
    matching = [item for item in catalog["items"] if item.get("candidate_id") == source.get("candidate_id")]
    _require(len(matching) == 1, "autonomous model candidate is not uniquely catalogued")
    candidate = matching[0]
    hint = {key: command.get(key) for key in ("action", "args", "preconditions", "expected_effects")}
    _require(_equal(candidate.get("hint"), hint), "autonomous model candidate intent changed")
    _require(command.get("risk") == candidate.get("risk"), "autonomous model candidate risk changed")
    _require(command.get("reason") == ("Model choice: " + str(candidate["summary"]))[:500], "autonomous model candidate summary changed")
    identity = {
        "session_id": command.get("session_id"), "source_frame_id": command.get("source_frame_id"),
        "state_hash": command.get("state_hash"), "request_id": source.get("request_id"),
        "contract_hash": candidate.get("contract_hash"), "run_id": command.get("run_id"),
        "controller_id": command.get("controller_id"),
    }
    expected_id = "cmd-model-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    _require(command.get("id") == expected_id, "autonomous model command id does not bind its original selection")


def assert_autonomous_authorization_context(context: AutonomousAuthorizationContext) -> None:
    """Recheck bytes and file identity, including pause/stop revocation."""

    for path, expected in context.files.items():
        observed = command_custody.read_stable_bytes(
            path, label=f"pinned autonomous input {path.name}", max_bytes=_MAX_DOCUMENT_BYTES,
        )
        _require(observed == expected, f"autonomous input changed under custody: {path.name}")


def load_autonomous_authorization_context(
    bridge_dir: pathlib.Path, command: Mapping[str, Any], *,
    latest_state: Mapping[str, Any] | None = None,
    frame: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> AutonomousAuthorizationContext:
    """Read a pinned, current context; callers must hold lifecycle custody."""

    root = pathlib.Path(os.path.abspath(str(bridge_dir)))
    _require(command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA, "legacy commands cannot receive autonomous authority")
    _require(command.get("status") == AUTONOMOUS_PENDING_STATUS, "autonomous command is not pending authorization")
    _require(set(command) == set(command_intent_projection(command)) | {"status"}, "autonomous pending command shape is invalid")
    _require(command.get("action") not in NON_MUTATING_ACTIONS, "autonomous mutation authorization requires a mutating action")
    files: Dict[pathlib.Path, Tuple[bytes, Tuple[int, int], int]] = {}
    documents: Dict[str, Dict[str, Any]] = {}
    for name in ("action-policy.json", "decision-frame.json", "latest_state.json", "autonomous-control.json"):
        path = root / name
        documents[name], files[path] = _read_object(path)
    current_policy = documents["action-policy.json"]
    current_frame = documents["decision-frame.json"]
    current_state = documents["latest_state.json"]
    control = documents["autonomous-control.json"]
    _require(current_policy.get("schema") == AUTONOMOUS_ACTION_POLICY_SCHEMA, "autonomous V2 policy is required")
    blockers = autonomous_policy_state_blockers(current_policy, current_state)
    _require(not blockers, "autonomous policy/state mismatch: " + ",".join(blockers))
    _require(trusted_proposal_state(current_state), "autonomous current state is not trustworthy")
    if policy is not None:
        _require(_equal(current_policy, policy), "supplied autonomous policy is no longer current")
    if frame is not None:
        _require(_equal(current_frame, frame), "supplied autonomous frame is not the committed current frame")
    if latest_state is not None:
        _require(trusted_proposal_state(dict(latest_state)), "supplied autonomous state is not trustworthy")
        _require(canonical_state_hash(dict(latest_state)) == canonical_state_hash(current_state), "supplied autonomous state is no longer current")
    archive_path = decision_frame_archive_path(root, command.get("source_frame_id"))
    archived, expected_archive, _archive_hash = load_archived_decision_frame(root, command.get("source_frame_id"))
    archive_document, files[archive_path] = _read_object(archive_path)
    _require(archive_path == expected_archive and _equal(archived, archive_document) and _equal(current_frame, archived), "autonomous source archive is not the committed frame")
    validate_approval_inputs(command, current_policy, current_frame, survival_state=current_state)
    validate_autonomous_control(control, current_policy, current_state)
    created = parse_snapshot_timestamp(command.get("created_at"))
    expires = parse_snapshot_timestamp(command.get("expires_at"))
    now = dt.datetime.now(dt.timezone.utc)
    _require(created is not None and expires is not None and created <= now < expires, "autonomous command is expired or has an invalid timestamp")
    _require(expires - created == dt.timedelta(seconds=current_policy["command_ttl_seconds"]), "autonomous command TTL differs from policy")
    hint = {key: command.get(key) for key in ("action", "args", "preconditions", "expected_effects")}
    _require(command_hint_is_current(current_state, hint), "autonomous action contracts or live preconditions are invalid")
    _validate_provenance(command, current_policy, current_frame, current_state)
    context = AutonomousAuthorizationContext(root, current_state, current_frame, current_policy, control, archive_path, files)
    assert_autonomous_authorization_context(context)
    return context


def validate_autonomous_pending_custody(
    bridge_dir: pathlib.Path, command: Mapping[str, Any],
    context: AutonomousAuthorizationContext,
) -> command_custody.PendingEnvelopeObservation:
    """Refuse second mutations, duplicate ownership, and unresolved outcomes."""

    blockers = (
        command_custody.lifecycle_recovery_blockers(bridge_dir)
        + command_custody.approval_recovery_blockers(bridge_dir)
        + command_custody.validation_control_blockers(bridge_dir)
    )
    _require(not blockers, "unresolved lifecycle/approval custody blocks autonomous authority")
    command_id = command.get("id")
    _require(isinstance(command_id, str) and _COMMAND_ID_RE.fullmatch(command_id) is not None, "autonomous command id is invalid")
    source = command_custody.queue_stage_path(bridge_dir, "pending", f"{command_id}.json")
    observation = command_custody.observe_pending_envelope(bridge_dir, source, expected_source=command)
    pipeline = command_pipeline_readiness(
        bridge_dir, latest_state=context.state, proposal_frame=context.frame, policy=context.policy,
    )
    permitted = {f"mutation_pending_model_selection:{command_id}", f"mutation_pending_selection:{command_id}"}
    unexpected = [item for item in pipeline["blockers"] if item not in permitted]
    _require(
        not unexpected
        and isinstance(pipeline.get("ambiguous_outcomes"), list)
        and not pipeline["ambiguous_outcomes"]
        and not pipeline.get("in_flight_mutations")
        and not pipeline.get("duplicate_ids")
        and not pipeline.get("invalid_records"),
        "another mutation or unresolved outcome blocks autonomous authority",
    )
    _require(pipeline.get("counts", {}).get("pending") == 1, "autonomous pending ownership is not exclusive")
    return observation


def authorize_autonomous_command(
    bridge_dir: pathlib.Path, command_id: str, *,
    latest_state: Mapping[str, Any], frame: Mapping[str, Any], policy: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return an approved document only after real durable approval custody."""

    _require(isinstance(command_id, str) and _COMMAND_ID_RE.fullmatch(command_id) is not None, "autonomous command id is invalid")
    from scripts.commit_command_approval import ApprovalCommitError, ApprovalOutcomeUnknown, commit

    temporary: pathlib.Path | None = None
    try:
        with command_custody.lifecycle_custody(bridge_dir):
            source = command_custody.queue_stage_path(bridge_dir, "pending", f"{command_id}.json")
            pending, pending_file = _read_object(source)
            _require(pending.get("id") == command_id, "autonomous pending id differs from its path")
            context = load_autonomous_authorization_context(
                bridge_dir, pending, latest_state=latest_state, frame=frame, policy=policy,
            )
            observation = validate_autonomous_pending_custody(bridge_dir, pending, context)
            _require((observation.payload, observation.identity, observation.mtime_ns) == pending_file, "autonomous pending identity changed before authorization")
            approved = build_approved_command(
                pending, context.policy, context.frame, approved_by="autonomous_controller",
                approved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                survival_state=context.state,
            )
            assert_autonomous_authorization_context(context)
            with tempfile.NamedTemporaryFile("wb", prefix="oni-autonomous-approved-", suffix=".json", delete=False) as handle:
                temporary = pathlib.Path(handle.name)
                handle.write((json.dumps(approved, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            args = argparse.Namespace(
                command=source,
                target=command_custody.queue_stage_path(bridge_dir, "approved", source.name),
                approved_payload=temporary, policy=context.bridge_dir / "action-policy.json",
                frame=context.archive_path,
                expected_command_sha256=command_custody.sha256_bytes(observation.payload),
                expected_policy_sha256=command_custody.sha256_bytes(context.files[context.bridge_dir / "action-policy.json"][0]),
                expected_frame_sha256=command_custody.sha256_bytes(context.files[context.archive_path][0]),
                autonomous_authorization_context=context, expected_pending_observation=observation,
            )
            commit(args)
            return copy.deepcopy(approved)
    except (ApprovalOutcomeUnknown, command_custody.CommandCustodyUnknown) as exc:
        raise AutonomousAuthorizationOutcomeUnknown(
            f"autonomous authorization outcome_unknown for {command_id}; reconcile the original id, do not replay: {exc}"
        ) from exc
    except AutonomousAuthorizationError:
        raise
    except (ApprovalCommitError, ApprovalReceiptError, DecisionFrameArchiveError, command_custody.CommandCustodyError, OSError, CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise AutonomousAuthorizationError(f"autonomous authorization refused for {command_id}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "AUTONOMOUS_CONTROL_SCHEMA", "AutonomousAuthorizationContext",
    "AutonomousAuthorizationError", "AutonomousAuthorizationOutcomeUnknown",
    "assert_autonomous_authorization_context", "authorize_autonomous_command",
    "load_autonomous_authorization_context", "validate_autonomous_control",
    "validate_autonomous_pending_custody",
]
