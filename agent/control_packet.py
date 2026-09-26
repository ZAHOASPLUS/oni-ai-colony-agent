"""Deterministic, non-executing control packets for one committed decision frame.

The packet is a bounded control-plane view only.  This module never reads or
writes the command queue and never enqueues, approves, claims, or executes a
command.  ``action_candidate`` means only that exactly one already-materialized
pending suggestion is bound to the supplied frame/state/policy context.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from agent.canonical_hash import CanonicalHashError, canonical_hash
from agent.decision import DecisionFrameError, validate_decision_frame


CONTROL_PACKET_SCHEMA = "oni.control-packet.v1"
COMMAND_PIPELINE_SCHEMA = "oni.command-pipeline-gate.v1"
COMMAND_PIPELINE_STAGES = (
    "pending",
    "approving",
    "approved",
    "executing",
    "confirming",
    "failed",
)
COMMAND_PIPELINE_KEYS = {
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
RISKS = {"high", "medium", "low", "unknown"}
_RISK_PRIORITY = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
_PHASE_PRIORITY = {"bootstrap": 0, "stabilize": 1, "expand": 2, "space": 3, "sustain": 4}

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,240}$")
_SENSITIVE_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|access[_-]?token|secret)\s*[:=])"
)
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var|etc)/)", re.IGNORECASE)
_FORBIDDEN_KEYS = {"args", "body", "path", "preconditions", "expected_effects", "effects"}
_PIPELINE_RECORD_KEYS = {
    "command_id",
    "action",
    "stage",
    "status",
    "classification",
    "mutating",
    "path",
}
_MUTATING_CLASSIFICATIONS = {
    "pending": {"pending_proposal"},
    "approving": {"approval_claimed"},
    "approved": {"approved_waiting_execution"},
    "executing": {"execution_claimed"},
    "confirming": {
        "awaiting_snapshot_confirmation",
        "confirmed_preparation",
        "ambiguous_confirmed_preparation",
        "ambiguous_confirmation_state",
    },
    "failed": {"outcome_unknown"},
}


class ControlPacketError(ValueError):
    """Raised when a control packet or one of its bindings is invalid."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlPacketError(message)


def _exact_keys(value: Any, keys: Iterable[str], label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    expected = set(keys)
    _require(set(value) == expected, f"{label} keys must be {sorted(expected)}")
    return value


def _safe_token(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        return None
    if _SENSITIVE_RE.search(value) or _PATH_RE.search(value):
        return None
    return value


def _packet_digest(packet: Mapping[str, Any]) -> str:
    return canonical_hash({key: value for key, value in packet.items() if key != "packet_id"})


def _gate_digest(gate: Mapping[str, Any]) -> str:
    return canonical_hash(dict(gate))


def _source_projection(frame: Mapping[str, Any]) -> Dict[str, Any]:
    source = frame["source"]
    policy = frame["projections"]["policy"]
    return {
        "frame_id": frame["frame_id"],
        "session_id": source["session_id"],
        "save_id_hash": source["save_id_hash"],
        "world_id": source["world_id"],
        "snapshot_id": source["snapshot_id"],
        "seq": source["seq"],
        "snapshot_seq": source["snapshot_seq"],
        "ts": source["ts"],
        "state_hash": source["state_hash"],
        "policy": {
            "valid": policy["valid"],
            "policy_hash": policy["policy_hash"],
        },
    }


def _validated_command_records(
    gate: Mapping[str, Any], counts: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    raw_records = gate.get("commands")
    _require(isinstance(raw_records, list), "pipeline commands must be a list")
    records: List[Dict[str, Any]] = []
    for index, value in enumerate(raw_records):
        item = _exact_keys(value, _PIPELINE_RECORD_KEYS, f"pipeline.commands[{index}]")
        command_id = item["command_id"]
        action = item["action"]
        stage = item["stage"]
        status = item["status"]
        classification = item["classification"]
        mutating = item["mutating"]
        path = item["path"]
        _require(_safe_token(command_id) == command_id, "pipeline command id is invalid")
        _require(_safe_token(action) == action, "pipeline command action is invalid")
        _require(stage in COMMAND_PIPELINE_STAGES, "pipeline command stage is invalid")
        _require(
            status is None or _safe_token(status) == status,
            "pipeline command status is invalid",
        )
        _require(isinstance(mutating, bool), "pipeline command mutating flag is invalid")
        expected_classifications = (
            _MUTATING_CLASSIFICATIONS[stage] if mutating else {"non_mutating"}
        )
        _require(
            classification in expected_classifications,
            "pipeline command classification disagrees with its stage",
        )
        expected_prefix = f"commands/{stage}/"
        _require(
            isinstance(path, str)
            and len(path) <= 512
            and path.startswith(expected_prefix)
            and len(path) > len(expected_prefix)
            and "\\" not in path
            and "/../" not in f"/{path}/",
            "pipeline command path is invalid",
        )
        records.append(dict(item))

    ordering = [
        (item["command_id"], item["stage"], item["path"])
        for item in records
    ]
    _require(ordering == sorted(ordering), "pipeline commands must be sorted")
    for stage in COMMAND_PIPELINE_STAGES:
        observed = sum(1 for item in records if item["stage"] == stage)
        _require(observed == counts[stage], f"pipeline count {stage} disagrees with commands")

    expected_in_flight = [
        item
        for item in records
        if item["mutating"]
        and item["stage"] in {"approving", "approved", "executing", "confirming"}
    ]
    _require(
        gate.get("in_flight_mutations") == expected_in_flight,
        "pipeline in-flight mutations disagree with commands",
    )
    return records


def _validated_gate_parts(
    gate: Mapping[str, Any], frame: Mapping[str, Any]
) -> Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    _exact_keys(gate, COMMAND_PIPELINE_KEYS, "pipeline")
    _require(gate.get("schema") == COMMAND_PIPELINE_SCHEMA, "pipeline gate schema is invalid")
    evaluation = _exact_keys(
        gate.get("evaluation_context"),
        {"phase", "proposal_frame_id", "state_hash", "policy_hash"},
        "pipeline.evaluation_context",
    )
    _require(evaluation["phase"] == "frame_bound", "pipeline gate is not frame-bound")
    _require(
        evaluation["proposal_frame_id"] == frame["frame_id"],
        "pipeline proposal frame binding mismatch",
    )
    _require(
        evaluation["state_hash"] == frame["source"]["state_hash"],
        "pipeline state binding mismatch",
    )
    _require(
        evaluation["policy_hash"] == frame["projections"]["policy"]["policy_hash"],
        "pipeline policy binding mismatch",
    )
    _require(isinstance(gate.get("ready"), bool), "pipeline gate ready flag is invalid")
    invalid_records = gate.get("invalid_records")
    _require(isinstance(invalid_records, list), "pipeline invalid_records must be a list")
    blockers = gate.get("blockers")
    _require(isinstance(blockers, list), "pipeline blockers must be a list")
    safe_blockers = [_safe_token(item) for item in blockers]
    _require(
        all(isinstance(item, str) and 0 < len(item) <= 512 for item in blockers),
        "pipeline blocker token is invalid",
    )
    # A structured I/O/JSON failure may contain an unsafe filename in its
    # diagnostic blocker.  It is hashed but never copied into the packet, whose
    # next_step is forced to null.  All healthy/readable gates remain strict.
    if not invalid_records:
        _require(all(item is not None for item in safe_blockers), "pipeline blocker token is unsafe")
    _require(blockers == sorted(set(blockers)), "pipeline blockers must be unique and sorted")

    readiness = _exact_keys(
        gate.get("readiness"),
        {"mutation_proposal", "mutation_confirmation"},
        "pipeline.readiness",
    )
    proposal = _exact_keys(
        readiness["mutation_proposal"], {"ready", "blockers"}, "pipeline.readiness.mutation_proposal"
    )
    confirmation = _exact_keys(
        readiness["mutation_confirmation"],
        {"ready", "blockers", "candidate_ids"},
        "pipeline.readiness.mutation_confirmation",
    )
    for label, entry in (("proposal", proposal), ("confirmation", confirmation)):
        _require(isinstance(entry["ready"], bool), f"pipeline {label} ready flag is invalid")
        _require(isinstance(entry["blockers"], list), f"pipeline {label} blockers are invalid")
        _require(
            all(isinstance(item, str) and 0 < len(item) <= 512 for item in entry["blockers"]),
            f"pipeline {label} blocker token is invalid",
        )
        if not invalid_records:
            _require(
                all(_safe_token(item) is not None for item in entry["blockers"]),
                f"pipeline {label} blocker token is unsafe",
            )
        _require(
            entry["blockers"] == sorted(set(entry["blockers"])),
            f"pipeline {label} blockers must be unique and sorted",
        )
    _require(proposal["blockers"] == blockers, "pipeline proposal blockers disagree")
    _require(proposal["ready"] is (not blockers), "pipeline proposal readiness is inconsistent")
    _require(gate["ready"] is proposal["ready"], "pipeline top-level readiness is inconsistent")
    _require(
        confirmation["ready"] is (not confirmation["blockers"]),
        "pipeline confirmation readiness is inconsistent",
    )
    _require(
        isinstance(confirmation["candidate_ids"], list)
        and confirmation["candidate_ids"] == sorted(set(confirmation["candidate_ids"]))
        and all(_safe_token(item) is not None for item in confirmation["candidate_ids"]),
        "pipeline confirmation candidates are invalid",
    )

    counts = _exact_keys(gate.get("counts"), COMMAND_PIPELINE_STAGES, "pipeline.counts")
    for stage in COMMAND_PIPELINE_STAGES:
        value = counts[stage]
        _require(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0,
            f"pipeline count {stage} is invalid",
        )
    pending_candidates = gate.get("pending_candidates")
    _require(isinstance(pending_candidates, list), "pipeline pending_candidates must be a list")
    _require(
        len(pending_candidates) <= counts["pending"],
        "pipeline pending candidate count exceeds pending commands",
    )
    records = _validated_command_records(gate, counts)
    expected_confirmation_candidates = sorted(
        {
            item["command_id"]
            for item in records
            if item["stage"] == "confirming"
            and item["mutating"]
            and item["classification"]
            in {"awaiting_snapshot_confirmation", "confirmed_preparation"}
        }
    )
    _require(
        confirmation["candidate_ids"] == expected_confirmation_candidates,
        "pipeline confirmation candidates disagree with commands",
    )

    # A failed mutating record is included in the scan only when its outcome is
    # unresolved.  Do not let a caller hide that durable ambiguity by deleting
    # the derived blocker or diagnostic while retaining a well-shaped record.
    ambiguous_outcomes = gate.get("ambiguous_outcomes")
    _require(isinstance(ambiguous_outcomes, list), "pipeline ambiguous_outcomes must be a list")
    for record in records:
        if not (
            record["stage"] == "failed"
            and record["mutating"] is True
            and record["classification"] == "outcome_unknown"
        ):
            continue
        command_id = record["command_id"]
        expected_blocker = f"mutation_outcome_unknown_unresolved:{command_id}"
        _require(expected_blocker in blockers, "pipeline hid an unresolved mutation outcome")
        _require(
            expected_blocker in confirmation["blockers"],
            "pipeline confirmation hid an unresolved mutation outcome",
        )
        expected_diagnostic = {
            "command_id": command_id,
            "classification": "outcome_unknown",
            "stages": ["failed"],
            "mutating": True,
        }
        _require(
            sum(item == expected_diagnostic for item in ambiguous_outcomes) == 1,
            "pipeline ambiguity diagnostic disagrees with failed command",
        )
    return proposal, confirmation, counts


def _pipeline_projection(gate: Mapping[str, Any], frame: Mapping[str, Any]) -> Dict[str, Any]:
    proposal, confirmation, counts = _validated_gate_parts(gate, frame)
    return {
        "schema": COMMAND_PIPELINE_SCHEMA,
        "gate_hash": _gate_digest(gate),
        "stage": "post_sync",
        "proposal_frame_evaluated": True,
        "proposal_frame_id": frame["frame_id"],
        "mutation_proposal_ready": proposal["ready"],
        "mutation_confirmation_ready": confirmation["ready"],
        "counts": {stage: counts[stage] for stage in COMMAND_PIPELINE_STAGES},
    }


def _candidate_summaries(gate: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw = gate.get("pending_candidates")
    _require(isinstance(raw, list), "pipeline pending_candidates must be a list")
    candidates: List[Dict[str, Any]] = []
    for index, value in enumerate(raw):
        item = _exact_keys(
            value,
            {
                "command_id",
                "action",
                "intent_hash",
                "risk",
                "requires_approval",
                "auto_execute",
            },
            f"pipeline.pending_candidates[{index}]",
        )
        _require(_safe_token(item["command_id"]) == item["command_id"], "candidate command id is invalid")
        _require(_safe_token(item["action"]) == item["action"], "candidate action is invalid")
        _require(
            isinstance(item["intent_hash"], str) and _HASH_RE.fullmatch(item["intent_hash"]) is not None,
            "candidate intent hash is invalid",
        )
        _require(item["risk"] in RISKS, "candidate risk is invalid")
        _require(item["requires_approval"] is True, "candidate approval flag must be true")
        _require(item["auto_execute"] is False, "candidate auto_execute flag must be false")
        matching_records = [
            record
            for record in gate["commands"]
            if record["stage"] == "pending"
            and record["command_id"] == item["command_id"]
            and record["action"] == item["action"]
            and record["status"] == "pending_human_approval"
            and record["mutating"] is True
            and record["classification"] == "pending_proposal"
            and record["path"] == f"commands/pending/{item['command_id']}.json"
        ]
        _require(
            len(matching_records) == 1,
            "candidate does not identify one current pending command",
        )
        candidates.append(dict(item))
    keys = [(item["command_id"], item["intent_hash"]) for item in candidates]
    _require(keys == sorted(keys), "pipeline pending_candidates must be sorted")
    return candidates


def _diagnostic_list(gate: Mapping[str, Any], key: str) -> List[Any]:
    value = gate.get(key)
    _require(isinstance(value, list), f"pipeline {key} must be a list")
    return value


def _action_candidate(frame: Mapping[str, Any], gate: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    candidates = _candidate_summaries(gate)
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    mutating_pending = [
        record
        for record in gate["commands"]
        if record["stage"] == "pending" and record["mutating"] is True
    ]
    if (
        len(mutating_pending) != 1
        or mutating_pending[0]["command_id"] != candidate["command_id"]
    ):
        return None
    if any(
        record["stage"] == "failed"
        and record["mutating"] is True
        and record["classification"] == "outcome_unknown"
        for record in gate["commands"]
    ):
        return None
    policy = frame["projections"]["policy"]
    health = frame["projections"]["health"]
    if not (
        frame["trust"]["status"] == "trusted"
        and frame["readiness"]["propose"]["ready"] is True
        and policy["valid"] is True
        and policy["suggestions_enabled"] is True
        and policy["actions_enabled"] is True
        and health["replay_mode"] is False
    ):
        return None
    for key in (
        "duplicate_ids",
        "ambiguous_outcomes",
        "stale_pending",
        "invalid_records",
        "in_flight_mutations",
    ):
        if _diagnostic_list(gate, key):
            return None
    expected_blocker = f"mutation_pending_selection:{candidate['command_id']}"
    if gate["blockers"] != [expected_blocker]:
        return None
    confirmation = gate["readiness"]["mutation_confirmation"]
    if confirmation["ready"] is not True or confirmation["blockers"]:
        return None
    return {
        "kind": "action_candidate",
        "command_id": candidate["command_id"],
        "action": candidate["action"],
        "intent_hash": candidate["intent_hash"],
        "risk": candidate["risk"],
        "requires_approval": True,
        "auto_execute": False,
    }


def _operator_recommendations(frame: Mapping[str, Any]) -> List[Dict[str, Any]]:
    projections = frame["projections"]
    result: List[Dict[str, Any]] = []
    plan = projections["plan"]
    if plan["valid"]:
        for item in plan["items"]:
            result.append(
                {
                    "kind": "operator_recommendation",
                    "source": "plan",
                    "identifier": item["incident_id"],
                    "risk": item["risk"],
                    "requires_approval": True,
                    "auto_execute": False,
                }
            )
    advice = projections["advice"]
    if advice["valid"]:
        for item in advice["items"]:
            result.append(
                {
                    "kind": "operator_recommendation",
                    "source": "advice",
                    "identifier": item["incident_id"],
                    "risk": item["risk"],
                    "requires_approval": True,
                    "auto_execute": False,
                }
            )
    strategy = projections["strategy"]
    if strategy["valid"]:
        strategy_items = sorted(
            strategy["recommendations"],
            key=lambda item: (
                _RISK_PRIORITY[item["risk"]],
                item["id"],
            ),
        )
        for item in strategy_items:
            result.append(
                {
                    "kind": "operator_recommendation",
                    "source": "strategy",
                    "identifier": item["id"],
                    "risk": item["risk"],
                    "requires_approval": True,
                    "auto_execute": False,
                }
            )
    # ``frame.projections.plan.items`` is already strictly validated in
    # priority order.  Preserve that ordering, then use the frame's canonical
    # advice and strategy orders as fallbacks; alphabetical re-sorting here
    # would silently discard planner priority.
    return result


def _observe_requests(frame: Mapping[str, Any], gate: Mapping[str, Any]) -> List[Dict[str, Any]]:
    pipeline_requests: List[Dict[str, Any]] = []
    source_and_trust_requests: List[Dict[str, Any]] = []
    domain_contract_requests: List[Dict[str, Any]] = []
    generic_frame_requests: List[Dict[str, Any]] = []
    generic_strategy_gate_requests: List[Dict[str, Any]] = []
    objective_requests: List[Dict[str, Any]] = []
    for blocker in gate["blockers"]:
        if _safe_token(blocker) is not None:
            pipeline_requests.append(
                {"kind": "observe_request", "source": "pipeline_blocker", "identifier": blocker}
            )
    for blocker in frame["blockers"]:
        code = blocker["code"]
        if _safe_token(code) is not None:
            if code in {
                "STATE_INVALID",
                "SOURCE_IDENTITY_MISSING",
                "STATE_STALE",
                "REPLAY_MODE",
                "HEALTH_INVALID",
                "HEALTH_NOT_OK",
                "COMMAND_PIPELINE_INVALID",
                "COMMAND_PIPELINE_MUTATION_BLOCKED",
                "OUTPUT_IDENTITY_MISSING",
                "OUTPUT_IDENTITY_MISMATCH",
            }:
                target = source_and_trust_requests
            elif code in {
                "ADVICE_INVALID",
                "INCIDENTS_INVALID",
                "PLAN_INVALID",
                "STRATEGY_INVALID",
                "POLICY_INVALID",
            }:
                target = domain_contract_requests
            elif code in {
                "STRATEGY_CRITICAL_GATE_NOT_PASSED",
                "SUSTAINABILITY_GATE_NOT_PASSED",
            }:
                target = generic_strategy_gate_requests
            else:
                target = generic_frame_requests
            target.append({"kind": "observe_request", "source": "frame_blocker", "identifier": code})
    strategy = frame["projections"]["strategy"]
    if strategy["valid"]:
        objectives = sorted(
            strategy["objectives"],
            key=lambda item: (
                -item["priority"],
                _PHASE_PRIORITY.get(item["phase"], 99),
                item["id"],
            ),
        )
        for objective in objectives:
            if objective["status"] in {"unknown", "blocked"} and _safe_token(objective["id"]):
                objective_requests.append(
                    {
                        "kind": "observe_request",
                        "source": "strategy_objective",
                        "identifier": objective["id"],
                    }
                )
    # Durable mutation-pipeline ambiguity is handled before general frame
    # observation. Specific survival objectives retain the strategy's numeric
    # priority (oxygen > food > sanitation > water > power > thermal) and are
    # more useful than the generic strategy-gate blockers they explain. The
    # public packet remains bounded to the identifier and exposes no score.
    ordered = (
        pipeline_requests
        + source_and_trust_requests
        + domain_contract_requests
        + objective_requests
        + generic_strategy_gate_requests
        + generic_frame_requests
    )
    unique: List[Dict[str, Any]] = []
    for item in ordered:
        if item not in unique:
            unique.append(item)
    return unique


def _select_next_step(frame: Mapping[str, Any], gate: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    # A structured invalid read is itself evidence that the post-sync view is
    # incomplete.  Publish a bound packet, but never select even an observation
    # token derived from the unsafe filename/path diagnostic.
    if _diagnostic_list(gate, "invalid_records"):
        _candidate_summaries(gate)
        return None

    candidate = _action_candidate(frame, gate)
    if candidate is not None:
        return candidate

    severe = any(
        _diagnostic_list(gate, key)
        for key in ("duplicate_ids", "ambiguous_outcomes", "stale_pending", "in_flight_mutations")
    )
    observations = _observe_requests(frame, gate)
    policy = frame["projections"]["policy"]
    if (
        severe
        or frame["trust"]["status"] != "trusted"
        or frame["projections"]["health"]["replay_mode"]
        or not policy["valid"]
        or policy["suggestions_enabled"] is False
    ):
        return observations[0] if observations else None

    # The unique action-candidate exception above is the only selection that
    # may coexist with its own mutation_pending_selection latch.  Every other
    # frame or pipeline blocker wins over plan/advice/strategy fallback so an
    # operator-owned, invalid-mode, stale, or otherwise blocked mutation can
    # never be obscured by a fresh recommendation.  The sole exception is
    # ACTIONS_DISABLED while suggestions remain enabled: a recommendation is
    # non-executing and therefore does not bypass the disabled action gate.
    priority_observations = [
        item
        for item in observations
        if not (
            item["source"] == "frame_blocker"
            and item["identifier"] == "ACTIONS_DISABLED"
            and policy["suggestions_enabled"] is True
        )
    ]
    if priority_observations:
        return priority_observations[0]

    recommendations = _operator_recommendations(frame)
    if recommendations:
        return recommendations[0]
    return observations[0] if observations else None


def build_control_packet(
    decision_frame: Mapping[str, Any],
    post_sync_gate: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build one deterministic packet from an exact frame and post-sync gate."""

    try:
        validate_decision_frame(decision_frame)
    except (DecisionFrameError, TypeError, ValueError, OverflowError) as exc:
        raise ControlPacketError("decision frame binding is invalid") from exc
    _require(isinstance(post_sync_gate, Mapping), "post-sync gate must be an object")
    frame = copy.deepcopy(dict(decision_frame))
    gate = copy.deepcopy(dict(post_sync_gate))
    packet: Dict[str, Any] = {
        "schema": CONTROL_PACKET_SCHEMA,
        "packet_id": "",
        "mode": "non_executing",
        "source": _source_projection(frame),
        "pipeline": _pipeline_projection(gate, frame),
        "next_step": _select_next_step(frame, gate),
    }
    packet["packet_id"] = _packet_digest(packet)
    validate_control_packet(packet, frame, gate)
    return packet


def _validate_next_step(value: Any) -> None:
    if value is None:
        return
    _require(isinstance(value, Mapping), "next_step must be an object or null")
    kind = value.get("kind")
    if kind == "observe_request":
        item = _exact_keys(value, {"kind", "source", "identifier"}, "next_step")
        _require(
            item["source"] in {"frame_blocker", "pipeline_blocker", "strategy_objective"},
            "observe_request source is invalid",
        )
        _require(_safe_token(item["identifier"]) == item["identifier"], "observe_request identifier is invalid")
        return
    if kind == "operator_recommendation":
        item = _exact_keys(
            value,
            {"kind", "source", "identifier", "risk", "requires_approval", "auto_execute"},
            "next_step",
        )
        _require(item["source"] in {"plan", "advice", "strategy"}, "recommendation source is invalid")
        _require(_safe_token(item["identifier"]) == item["identifier"], "recommendation identifier is invalid")
        _require(item["risk"] in RISKS, "recommendation risk is invalid")
        _require(item["requires_approval"] is True, "recommendation approval flag must be true")
        _require(item["auto_execute"] is False, "recommendation auto_execute flag must be false")
        return
    if kind == "action_candidate":
        item = _exact_keys(
            value,
            {
                "kind",
                "command_id",
                "action",
                "intent_hash",
                "risk",
                "requires_approval",
                "auto_execute",
            },
            "next_step",
        )
        _require(_safe_token(item["command_id"]) == item["command_id"], "action candidate id is invalid")
        _require(_safe_token(item["action"]) == item["action"], "action candidate action is invalid")
        _require(isinstance(item["intent_hash"], str) and _HASH_RE.fullmatch(item["intent_hash"]), "action candidate intent hash is invalid")
        _require(item["risk"] in RISKS, "action candidate risk is invalid")
        _require(item["requires_approval"] is True, "action candidate approval flag must be true")
        _require(item["auto_execute"] is False, "action candidate auto_execute flag must be false")
        return
    raise ControlPacketError("next_step kind is invalid")


def _reject_forbidden_content(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value) & _FORBIDDEN_KEYS
        _require(not forbidden, f"packet contains forbidden keys {sorted(forbidden)}")
        for item in value.values():
            _reject_forbidden_content(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_content(item)


def validate_control_packet(
    packet: Mapping[str, Any],
    decision_frame: Mapping[str, Any],
    post_sync_gate: Mapping[str, Any],
) -> None:
    """Validate exact schema, digest, and all external frame/gate bindings."""

    try:
        validate_decision_frame(decision_frame)
    except (DecisionFrameError, TypeError, ValueError, OverflowError) as exc:
        raise ControlPacketError("decision frame binding is invalid") from exc
    top = _exact_keys(
        packet,
        {"schema", "packet_id", "mode", "source", "pipeline", "next_step"},
        "packet",
    )
    _require(top["schema"] == CONTROL_PACKET_SCHEMA, "packet schema is invalid")
    _require(top["mode"] == "non_executing", "packet mode must be non_executing")
    _require(isinstance(top["packet_id"], str) and _HASH_RE.fullmatch(top["packet_id"]), "packet id is invalid")
    try:
        expected_packet_id = _packet_digest(top)
    except (CanonicalHashError, TypeError, ValueError, OverflowError) as exc:
        raise ControlPacketError("packet is not canonical") from exc
    _require(top["packet_id"] == expected_packet_id, "packet id does not match payload")

    source = _exact_keys(
        top["source"],
        {
            "frame_id",
            "session_id",
            "save_id_hash",
            "world_id",
            "snapshot_id",
            "seq",
            "snapshot_seq",
            "ts",
            "state_hash",
            "policy",
        },
        "packet.source",
    )
    _exact_keys(source["policy"], {"valid", "policy_hash"}, "packet.source.policy")
    _require(dict(source) == _source_projection(decision_frame), "packet source binding mismatch")

    pipeline = _exact_keys(
        top["pipeline"],
        {
            "schema",
            "gate_hash",
            "stage",
            "proposal_frame_evaluated",
            "proposal_frame_id",
            "mutation_proposal_ready",
            "mutation_confirmation_ready",
            "counts",
        },
        "packet.pipeline",
    )
    _exact_keys(pipeline["counts"], COMMAND_PIPELINE_STAGES, "packet.pipeline.counts")
    expected_pipeline = _pipeline_projection(post_sync_gate, decision_frame)
    _require(dict(pipeline) == expected_pipeline, "packet pipeline binding mismatch")
    _require(pipeline["stage"] == "post_sync", "packet pipeline stage is invalid")
    _require(pipeline["proposal_frame_evaluated"] is True, "packet pipeline must evaluate the proposal frame")
    _require(pipeline["proposal_frame_id"] == source["frame_id"], "packet frame ids disagree")

    _validate_next_step(top["next_step"])
    expected_next_step = _select_next_step(decision_frame, post_sync_gate)
    _require(top["next_step"] == expected_next_step, "packet next_step binding mismatch")
    _reject_forbidden_content(top)
    serialized = json.dumps(top, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    _require(_SENSITIVE_RE.search(serialized) is None, "packet contains credential-shaped text")
    _require(_PATH_RE.search(serialized) is None, "packet contains a local path")


__all__ = [
    "COMMAND_PIPELINE_STAGES",
    "CONTROL_PACKET_SCHEMA",
    "ControlPacketError",
    "build_control_packet",
    "validate_control_packet",
]
