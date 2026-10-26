from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import re
import sys
from typing import Any, Dict, Mapping, Optional, Sequence

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_outcomes import command_intent_hash
from agent.action_policy import (
    ACTION_POLICY_SCHEMA,
    AUTONOMOUS_ACTION_POLICY_SCHEMA,
    action_policy_is_valid,
    autonomous_identifier_is_valid,
    autonomous_policy_state_blockers,
)
from agent.canonical_hash import (
    CanonicalHashError,
    canonical_bytes as _contract_canonical_bytes,
    canonical_hash as _contract_canonical_hash,
    loads_strict,
)
from agent.decision import DecisionFrameError, validate_decision_frame
from agent.state_quality import (
    BRIDGE_PUBLISH_MARGIN_SECONDS,
    canonical_state_hash,
    snapshot_trust_blockers,
)


APPROVAL_RECEIPT_SCHEMA = "oni.approval-receipt.v1"
AUTONOMOUS_APPROVAL_RECEIPT_SCHEMA = "oni.approval-receipt.v2"
AUTONOMOUS_COMMAND_SCHEMA = "oni.command.v4"
NON_MUTATING_ACTIONS = frozenset({"noop", "focus_note"})
OPERATOR_MUTATING_MODES = frozenset({"runtime_validation", "planned"})
# Bridge-published model choice (agent.model_loop, review 2026-09-26 M1).
MODEL_SELECTED_MODE = "model_selected"
SUPPORTED_MUTATING_MODES = OPERATOR_MUTATING_MODES | {"suggested", MODEL_SELECTED_MODE}

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECEIPT_KEYS = frozenset(
    {
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
)
_SOURCE_KEYS = frozenset(
    {"session_id", "save_id_hash", "world_id", "snapshot_id", "state_hash"}
)
_AUTONOMOUS_RECEIPT_KEYS = _RECEIPT_KEYS | {"run_id", "controller_id"}


class ApprovalReceiptError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ApprovalReceiptError(message)


def _canonical_json(value: Any) -> bytes:
    try:
        return _contract_canonical_bytes(value)
    except CanonicalHashError as exc:
        raise ApprovalReceiptError("approval payload is outside the canonical hash contract") from exc


def canonical_hash(value: Any) -> str:
    try:
        return _contract_canonical_hash(value)
    except CanonicalHashError as exc:
        raise ApprovalReceiptError("approval payload is outside the canonical hash contract") from exc


def _command_intent_hash(command: Mapping[str, Any]) -> str:
    try:
        return command_intent_hash(command)
    except CanonicalHashError as exc:
        raise ApprovalReceiptError("command intent is outside the canonical hash contract") from exc


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
    return type(left) is type(right) and left == right


def _parse_approval_time(value: Any) -> dt.datetime:
    _require(isinstance(value, str) and bool(value), "approved_at is required")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalReceiptError("approved_at is invalid") from exc
    _require(parsed.tzinfo is not None, "approved_at must include a UTC offset")
    return parsed


def _frame_source(frame: Mapping[str, Any]) -> Dict[str, Any]:
    source = frame.get("source")
    _require(isinstance(source, Mapping), "decision frame source is missing")
    return {
        "session_id": source.get("session_id"),
        "save_id_hash": source.get("save_id_hash"),
        "world_id": source.get("world_id"),
        "snapshot_id": source.get("snapshot_id"),
        "state_hash": source.get("state_hash"),
    }


def _command_source(command: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": command.get("session_id"),
        "save_id_hash": command.get("save_id_hash"),
        "world_id": command.get("world_id"),
        "snapshot_id": command.get("source_snapshot_id"),
        "state_hash": command.get("state_hash"),
    }


def _validate_authorizing_policy(
    command: Mapping[str, Any], policy: Mapping[str, Any]
) -> None:
    _require(action_policy_is_valid(policy), "action policy contract is invalid")
    autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
    _require(
        policy.get("schema") == (
            AUTONOMOUS_ACTION_POLICY_SCHEMA if autonomous else ACTION_POLICY_SCHEMA
        ),
        "command and action policy authorization versions are mismatched",
    )
    _require(policy.get("actions_enabled") is True, "actions are disabled by local policy")
    actions = policy.get("allowed_actions")
    saves = policy.get("allowed_save_hashes")
    _require(isinstance(actions, list), "policy allowed_actions is invalid")
    _require(isinstance(saves, list), "policy allowed_save_hashes is invalid")
    _require(command.get("action") in actions, "command action is not allowed by local policy")
    _require(command.get("save_id_hash") in saves, "command save is not allowed by local policy")
    if autonomous:
        _require(command.get("requires_approval") is True, "autonomous command requires authorization")
        _require(
            command.get("mode") in ("suggested", MODEL_SELECTED_MODE),
            "autonomous command mode is unsupported",
        )
        for key in ("run_id", "controller_id"):
            _require(
                autonomous_identifier_is_valid(command.get(key))
                and command.get(key) == policy.get(key),
                f"autonomous command {key} does not match policy",
            )
        _require(command.get("session_id") == policy.get("session_id"), "autonomous session does not match policy")
        _require(
            type(command.get("world_id")) is int and command.get("world_id") == policy.get("world_id"),
            "autonomous world does not match policy",
        )
        _require(saves == [command.get("save_id_hash")], "autonomous save identity is not exclusive")


def _validate_autonomous_snapshot(
    command: Mapping[str, Any],
    policy: Mapping[str, Any],
    frame: Mapping[str, Any],
    state: Mapping[str, Any],
) -> None:
    from agent.command_queue import trusted_proposal_state

    _require(trusted_proposal_state(dict(state)), "autonomous latest state is not trustworthy")
    # The authorization runs inside the Bridge's own publish cycle, so it must
    # allow the same publish lag the Bridge and the model loop allow.  Without
    # the margin the threshold here was 5s * 3 = 15s while the loop's was 45s, so
    # a snapshot the loop accepted was refused as ``snapshot_stale`` and the
    # command sat in pending/ until its TTL expired (2026-10-26 round 26).
    blockers = autonomous_policy_state_blockers(policy, state) + snapshot_trust_blockers(
        dict(state), bridge_margin_seconds=BRIDGE_PUBLISH_MARGIN_SECONDS
    )
    _require(not blockers, "autonomous startup/state binding refused: " + ",".join(blockers))
    source = frame.get("source") if isinstance(frame.get("source"), Mapping) else {}
    expected = {
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        "snapshot_id": state.get("snapshot_id"),
        "seq": state.get("seq"),
        "snapshot_seq": state.get("snapshot_seq"),
        "ts": state.get("ts"),
        "state_hash": canonical_state_hash(dict(state)),
    }
    _require(_strict_json_equal(dict(source), expected), "autonomous latest state does not exactly match committed frame")
    _require(command.get("state_hash") == expected["state_hash"], "autonomous command state binding is stale")


def _validate_frame_proposal_authority(
    command: Mapping[str, Any], propose: Any
) -> None:
    _require(isinstance(propose, Mapping), "decision frame proposal readiness is missing")
    ready = propose.get("ready")
    blockers = propose.get("blockers")
    _require(
        isinstance(ready, bool) and isinstance(blockers, list),
        "decision frame proposal readiness is invalid",
    )
    mode = command.get("mode")
    _require(isinstance(mode, str) and mode in SUPPORTED_MUTATING_MODES, "mutating command mode is unsupported")

    if ready is True and blockers == []:
        return
    if mode in OPERATOR_MUTATING_MODES and ready is False and blockers == ["SUGGESTIONS_DISABLED"]:
        return
    if mode == "suggested":
        raise ApprovalReceiptError("suggested command requires a proposal-ready decision frame")
    if mode == MODEL_SELECTED_MODE:
        # A model choice is a Bridge proposal, so SUGGESTIONS_DISABLED is not
        # an operator exemption for it.
        raise ApprovalReceiptError("model-selected command requires a proposal-ready decision frame")
    raise ApprovalReceiptError(
        "operator command proposal blockers must be exactly SUGGESTIONS_DISABLED"
    )


def validate_approval_inputs(
    command: Mapping[str, Any],
    policy: Mapping[str, Any],
    frame: Mapping[str, Any],
    *,
    survival_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate a V3 human or V4 autonomous mutating authorization context.

    ``survival_state`` is the snapshot the frame was built from (the Bridge's
    ``latest_state.json`` while it is still that snapshot).  When given, a
    suggested survival pause is re-arbitrated against it instead of trusting
    the frame's certificate alone (review S1); a state that is not the
    frame's snapshot rejects the approval.
    """

    _require(command.get("schema") in ("oni.command.v3", AUTONOMOUS_COMMAND_SCHEMA), "command authorization schema is unsupported")
    _require(
        isinstance(command.get("action"), str)
        and command.get("action") not in NON_MUTATING_ACTIONS,
        "approval receipts are required only for mutating commands",
    )
    source_frame_id = command.get("source_frame_id")
    _require(
        isinstance(source_frame_id, str) and _HASH_RE.fullmatch(source_frame_id) is not None,
        "mutating command source_frame_id is missing or invalid",
    )
    try:
        validate_decision_frame(frame)
    except (DecisionFrameError, TypeError, ValueError, OverflowError) as exc:
        raise ApprovalReceiptError(f"decision frame is invalid: {exc}") from exc
    _require(frame.get("frame_id") == source_frame_id, "decision frame does not match source_frame_id")

    trust = frame.get("trust")
    readiness = frame.get("readiness")
    propose = readiness.get("propose") if isinstance(readiness, Mapping) else None
    _require(isinstance(trust, Mapping) and trust.get("status") == "trusted", "decision frame is not trusted")
    _validate_frame_proposal_authority(command, propose)

    expected_source = _command_source(command)
    actual_source = _frame_source(frame)
    _require(
        _strict_json_equal(actual_source, expected_source),
        "decision frame source does not exactly match command source",
    )
    _validate_authorizing_policy(command, policy)
    if command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA and survival_state is not None:
        _validate_autonomous_snapshot(command, policy, frame, survival_state)
    policy_hash = canonical_hash(policy)
    command_source = command.get("source")
    _require(isinstance(command_source, Mapping), "mutating command source is missing")
    _require(
        command_source.get("policy_hash") == policy_hash,
        "mutating command source does not bind the current action policy",
    )
    projections = frame.get("projections")
    policy_projection = (
        projections.get("policy") if isinstance(projections, Mapping) else None
    )
    _require(
        isinstance(policy_projection, Mapping)
        and policy_projection.get("policy_hash") == policy_hash,
        "decision frame does not bind the current action policy",
    )
    if command.get("mode") == "suggested" and command.get("action") == "set_paused":
        from agent import rules as agent_rules

        survival_blockers = agent_rules.survival_suggested_command_frame_blockers(
            command,
            frame,
            state=survival_state,
        )
        _require(
            not survival_blockers,
            "suggested pause lacks a valid survival certificate: "
            + ",".join(survival_blockers),
        )
    return actual_source


def issue_approval_receipt(
    command: Mapping[str, Any],
    policy: Mapping[str, Any],
    frame: Mapping[str, Any],
    *,
    approved_by: str,
    approved_at: str,
    survival_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
    expected_authority = "autonomous_controller" if autonomous else "local_human"
    _require(approved_by == expected_authority, f"approved_by must be {expected_authority}")
    if autonomous:
        _require(isinstance(survival_state, Mapping), "autonomous receipt issuance requires the actual latest state")
    _parse_approval_time(approved_at)
    source = validate_approval_inputs(command, policy, frame, survival_state=survival_state)
    receipt: Dict[str, Any] = {
        "schema": AUTONOMOUS_APPROVAL_RECEIPT_SCHEMA if autonomous else APPROVAL_RECEIPT_SCHEMA,
        "command_id": command.get("id"),
        "command_intent_hash": _command_intent_hash(command),
        "source_frame_id": command.get("source_frame_id"),
        "source": copy.deepcopy(source),
        "policy_hash": canonical_hash(policy),
        "approved_by": approved_by,
        "approved_at": approved_at,
        "receipt_hash": "",
    }
    if autonomous:
        receipt["run_id"] = command.get("run_id")
        receipt["controller_id"] = command.get("controller_id")
    receipt["receipt_hash"] = canonical_hash(
        {key: value for key, value in receipt.items() if key != "receipt_hash"}
    )
    validate_approval_receipt(command, policy, frame, receipt, survival_state=survival_state)
    return receipt


def build_approved_command(
    command: Mapping[str, Any],
    policy: Mapping[str, Any],
    frame: Mapping[str, Any],
    *,
    approved_by: str,
    approved_at: str,
    survival_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the exact approved document without a lossy JSON language round-trip."""

    receipt = issue_approval_receipt(
        command,
        policy,
        frame,
        approved_by=approved_by,
        approved_at=approved_at,
        survival_state=survival_state,
    )
    approved = copy.deepcopy(dict(command))
    approved["status"] = "approved"
    approved["approved_at"] = approved_at
    approved["approved_by"] = approved_by
    approved["approval_receipt"] = receipt
    validate_approval_receipt(approved, policy, frame, survival_state=survival_state)
    return approved


def validate_approval_receipt(
    command: Mapping[str, Any],
    policy: Mapping[str, Any],
    frame: Mapping[str, Any],
    receipt: Mapping[str, Any] | None = None,
    *,
    survival_state: Optional[Mapping[str, Any]] = None,
) -> None:
    """Fail closed if an approved command, frame, policy, or receipt changed."""

    candidate = receipt if receipt is not None else command.get("approval_receipt")
    autonomous = command.get("schema") == AUTONOMOUS_COMMAND_SCHEMA
    _require(isinstance(candidate, Mapping), "approval_receipt is missing")
    _require(set(candidate) == (_AUTONOMOUS_RECEIPT_KEYS if autonomous else _RECEIPT_KEYS), "approval_receipt keys are invalid")
    _require(
        candidate.get("schema") == (AUTONOMOUS_APPROVAL_RECEIPT_SCHEMA if autonomous else APPROVAL_RECEIPT_SCHEMA),
        "approval_receipt schema is invalid",
    )
    if autonomous:
        for key in ("run_id", "controller_id"):
            _require(
                autonomous_identifier_is_valid(candidate.get(key))
                and candidate.get(key) == command.get(key) == policy.get(key),
                f"approval_receipt {key} mismatch",
            )
    _require(candidate.get("command_id") == command.get("id"), "approval_receipt command_id mismatch")
    _require(
        candidate.get("command_intent_hash") == _command_intent_hash(command),
        "command changed after approval",
    )
    _require(
        isinstance(candidate.get("command_intent_hash"), str)
        and _HASH_RE.fullmatch(str(candidate.get("command_intent_hash"))) is not None,
        "approval_receipt command_intent_hash is invalid",
    )
    _require(
        candidate.get("source_frame_id") == command.get("source_frame_id"),
        "approval_receipt source_frame_id mismatch",
    )
    source = candidate.get("source")
    _require(isinstance(source, Mapping) and set(source) == _SOURCE_KEYS, "approval_receipt source is invalid")
    expected_source = validate_approval_inputs(command, policy, frame, survival_state=survival_state)
    _require(_strict_json_equal(source, expected_source), "approval_receipt source mismatch")
    _require(candidate.get("policy_hash") == canonical_hash(policy), "policy changed after approval")
    _require(
        isinstance(candidate.get("policy_hash"), str)
        and _HASH_RE.fullmatch(str(candidate.get("policy_hash"))) is not None,
        "approval_receipt policy_hash is invalid",
    )
    _require(
        candidate.get("approved_by") == ("autonomous_controller" if autonomous else "local_human"),
        "approval_receipt approved_by is invalid",
    )
    _parse_approval_time(candidate.get("approved_at"))
    if "approved_by" in command:
        _require(command.get("approved_by") == candidate.get("approved_by"), "approved_by audit mismatch")
    if "approved_at" in command:
        _require(command.get("approved_at") == candidate.get("approved_at"), "approved_at audit mismatch")
    receipt_hash = candidate.get("receipt_hash")
    _require(
        isinstance(receipt_hash, str) and _HASH_RE.fullmatch(receipt_hash) is not None,
        "approval_receipt receipt_hash is invalid",
    )
    expected_hash = canonical_hash(
        {key: value for key, value in candidate.items() if key != "receipt_hash"}
    )
    _require(receipt_hash == expected_hash, "approval_receipt was tampered")


def _read_json(path: pathlib.Path, label: str) -> Dict[str, Any]:
    try:
        value = loads_strict(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, CanonicalHashError) as exc:
        raise ApprovalReceiptError(f"cannot read {label}: {exc}") from exc
    _require(isinstance(value, dict), f"{label} must contain a JSON object")
    return value


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue or validate a bound ONI approval receipt.")
    parser.add_argument("mode", choices=("issue", "approve", "validate"))
    parser.add_argument("--command", required=True, type=pathlib.Path)
    parser.add_argument("--policy", required=True, type=pathlib.Path)
    parser.add_argument("--frame", required=True, type=pathlib.Path)
    parser.add_argument(
        "--state",
        type=pathlib.Path,
        help="the frame's snapshot (latest_state.json); re-arbitrates a suggested survival pause",
    )
    parser.add_argument("--approved-by", default="local_human")
    parser.add_argument("--approved-at")
    args = parser.parse_args(argv)

    try:
        command = _read_json(args.command, "command")
        policy = _read_json(args.policy, "policy")
        frame = _read_json(args.frame, "decision frame")
        survival_state = _read_json(args.state, "state") if args.state is not None else None
        if args.mode in {"issue", "approve"}:
            _require(bool(args.approved_at), f"--approved-at is required in {args.mode} mode")
            if args.mode == "issue":
                output = issue_approval_receipt(
                    command,
                    policy,
                    frame,
                    approved_by=args.approved_by,
                    approved_at=str(args.approved_at),
                    survival_state=survival_state,
                )
            else:
                output = build_approved_command(
                    command,
                    policy,
                    frame,
                    approved_by=args.approved_by,
                    approved_at=str(args.approved_at),
                    survival_state=survival_state,
                )
            # ASCII-only output: approve-command.ps1 captures this through the
            # console code page (GBK under Windows PowerShell 5.1), which
            # corrupted non-ASCII values such as Chinese duplicant names when
            # Python wrote UTF-8.  Receipt hashes cover parsed values, so the
            # escapes change no hash; the commit helper re-serializes the file.
            sys.stdout.write(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            sys.stdout.write("\n")
        else:
            validate_approval_receipt(command, policy, frame, survival_state=survival_state)
            sys.stdout.write("approval receipt valid\n")
    except ApprovalReceiptError as exc:
        sys.stderr.write(f"approval receipt rejected: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "APPROVAL_RECEIPT_SCHEMA",
    "AUTONOMOUS_APPROVAL_RECEIPT_SCHEMA",
    "ApprovalReceiptError",
    "build_approved_command",
    "canonical_hash",
    "issue_approval_receipt",
    "validate_approval_inputs",
    "validate_approval_receipt",
]
