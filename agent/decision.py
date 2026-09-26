"""Atomic, fail-closed decision frames for the ONI automation stack.

The bridge historically writes several independently useful JSON documents.
This module does not execute, enqueue, approve, or write anything.  It projects
one coherent, bounded view from those documents and binds every conclusion to
one state identity.  Callers may then publish the returned dictionary with a
single atomic file replacement.

Only explicitly whitelisted fields are projected.  In particular, command
objects, action arguments, free-form paths, credentials, and strategy prose do
not enter the frame.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.action_policy import project_action_policy
from agent.canonical_hash import CanonicalHashError, canonical_hash
from agent.state_quality import canonical_state_hash, parse_snapshot_timestamp, parse_timestamp


DECISION_FRAME_SCHEMA = "oni.decision-frame.v2"
COMMAND_PIPELINE_SCHEMA = "oni.command-pipeline-gate.v1"
READINESS_STAGES = ("observe", "advise", "propose", "advance", "claim_sustainable")
OUTPUT_SOURCES = ("health", "advice", "incidents", "plan", "strategy", "policy")
IDENTITY_FIELDS = ("session_id", "snapshot_id", "state_hash")
COMMAND_PIPELINE_STAGES = ("pending", "approving", "approved", "executing", "confirming", "failed")

OBJECTIVE_STATUSES = {"satisfied", "at_risk", "unknown", "blocked", "deferred"}
STRATEGY_STATUSES = {"ok", "degraded", "at_risk", "blocked"}
STRATEGY_CONFIDENCES = {"high", "medium", "low", "blocked"}
STRATEGY_PHASES = {"bootstrap", "stabilize", "expand", "space", "sustain"}
STRATEGY_OPERATING_MODES = {"observe", "execute", "recover", "emergency"}
STRATEGY_READINESS_STATUSES = {"ready", "not_ready", "unknown", "blocked"}
RISKS = {"high", "medium", "low", "unknown"}
INCIDENT_PROJECTION_KEYS = {
    "incident_id",
    "status",
    "risk",
    "telemetry_state",
    "session_id",
    "snapshot_id",
    "first_seq",
    "last_seq",
    "first_snapshot_seq",
    "last_snapshot_seq",
    "consecutive_samples",
    "first_seen",
    "last_seen",
}

# Advancing the colony is deliberately stricter than merely making a proposal.
# Every present-tense safety gate must be observed as satisfied first.
ADVANCE_GATE_IDS = (
    "oxygen_security",
    "food_reserve",
    "sanitation",
    "water_resilience",
    "power_resilience",
    "morale_and_stress",
    "research_progress",
    "thermal_safety",
)
SUSTAINABILITY_GATE_IDS = ADVANCE_GATE_IDS + (
    "renewable_food",
    "renewable_oxygen",
    "renewable_water",
    "renewable_power",
    "fault_tolerance",
    "space_progress",
)
ALL_GATE_IDS = SUSTAINABILITY_GATE_IDS

BLOCKER_CODES = {
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
    "ADVICE_INVALID",
    "INCIDENTS_INVALID",
    "PLAN_INVALID",
    "STRATEGY_INVALID",
    "STRATEGY_STATUS_NOT_READY",
    "STRATEGY_CONFIDENCE_LOW",
    "STRATEGY_CONFIDENCE_BLOCKED",
    "STRATEGY_CONFIDENCE_NOT_HIGH",
    "STRATEGY_WINDOW_NOT_VERIFIED",
    "STRATEGY_CRITICAL_GATE_NOT_PASSED",
    "STRATEGY_PHASE_NOT_SUSTAIN",
    "SUSTAINABILITY_GATE_NOT_PASSED",
    "INDEFINITE_SURVIVAL_NOT_PROVEN",
    "POLICY_INVALID",
    "SUGGESTIONS_DISABLED",
    "ACTIONS_DISABLED",
}

_BASE_BLOCKERS = {
    "STATE_INVALID",
    "SOURCE_IDENTITY_MISSING",
    "STATE_STALE",
    "HEALTH_INVALID",
    "HEALTH_NOT_OK",
    "OUTPUT_IDENTITY_MISSING",
    "OUTPUT_IDENTITY_MISMATCH",
}
_ADVISE_BLOCKERS = _BASE_BLOCKERS | {"ADVICE_INVALID", "INCIDENTS_INVALID"}
_PROPOSE_BLOCKERS = _ADVISE_BLOCKERS | {
    "COMMAND_PIPELINE_INVALID",
    "COMMAND_PIPELINE_MUTATION_BLOCKED",
    "PLAN_INVALID",
    "POLICY_INVALID",
    "SUGGESTIONS_DISABLED",
    "REPLAY_MODE",
}
_ADVANCE_BLOCKERS = _BASE_BLOCKERS | {
    "COMMAND_PIPELINE_INVALID",
    "COMMAND_PIPELINE_MUTATION_BLOCKED",
    "PLAN_INVALID",
    "STRATEGY_INVALID",
    "STRATEGY_STATUS_NOT_READY",
    "STRATEGY_CONFIDENCE_LOW",
    "STRATEGY_CONFIDENCE_BLOCKED",
    "STRATEGY_WINDOW_NOT_VERIFIED",
    "STRATEGY_CRITICAL_GATE_NOT_PASSED",
    "POLICY_INVALID",
    "ACTIONS_DISABLED",
    "REPLAY_MODE",
}
_CLAIM_BLOCKERS = _BASE_BLOCKERS | {
    "STRATEGY_INVALID",
    "STRATEGY_STATUS_NOT_READY",
    "STRATEGY_CONFIDENCE_LOW",
    "STRATEGY_CONFIDENCE_BLOCKED",
    "STRATEGY_CONFIDENCE_NOT_HIGH",
    "STRATEGY_WINDOW_NOT_VERIFIED",
    "STRATEGY_PHASE_NOT_SUSTAIN",
    "SUSTAINABILITY_GATE_NOT_PASSED",
    "INDEFINITE_SURVIVAL_NOT_PROVEN",
    "REPLAY_MODE",
}
_STAGE_BLOCKERS = {
    "observe": _BASE_BLOCKERS,
    "advise": _ADVISE_BLOCKERS,
    "propose": _PROPOSE_BLOCKERS,
    "advance": _ADVANCE_BLOCKERS,
    "claim_sustainable": _CLAIM_BLOCKERS,
}

_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SAFE_STATUS_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,160}$")
_SAVE_HASH_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_STATE_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PIPELINE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,240}$")
_SENSITIVE_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|access[_-]?token|secret)\s*[:=])"
)
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var|etc)/)", re.IGNORECASE)


class DecisionFrameError(ValueError):
    """Raised when a decision frame violates its public contract."""


def _mapping(value: Any) -> Dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _safe_identifier(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        return None
    if _SENSITIVE_RE.search(value) or _PATH_RE.search(value):
        return None
    return value


def _safe_status(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _SAFE_STATUS_RE.fullmatch(value):
        return None
    if _SENSITIVE_RE.search(value) or _PATH_RE.search(value):
        return None
    return value


def _safe_session(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _SESSION_RE.fullmatch(value):
        return None
    if _SENSITIVE_RE.search(value) or _PATH_RE.search(value):
        return None
    return value


def _safe_world(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _SESSION_RE.fullmatch(value) and not _SENSITIVE_RE.search(value):
        return value
    return None


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _source_identity(state: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    session_id = _safe_session(state.get("session_id"))
    save_hash_value = state.get("save_id_hash")
    save_id_hash = save_hash_value if isinstance(save_hash_value, str) and _SAVE_HASH_RE.fullmatch(save_hash_value) else None
    snapshot_id = _safe_session(state.get("snapshot_id"))
    world_id = _safe_world(state.get("world_id"))
    seq_value = state.get("seq")
    seq = seq_value if isinstance(seq_value, int) and not isinstance(seq_value, bool) and seq_value > 0 else None
    ts_value = state.get("ts")
    ts = (
        ts_value
        if isinstance(ts_value, str) and parse_snapshot_timestamp(ts_value) is not None
        else None
    )
    try:
        state_hash = canonical_state_hash(dict(state))
    except (TypeError, ValueError, OverflowError):
        state_hash = None

    source = {
        "session_id": session_id,
        "save_id_hash": save_id_hash,
        "world_id": world_id,
        "snapshot_id": snapshot_id,
        "seq": seq,
        "snapshot_seq": (
            state.get("snapshot_seq")
            if isinstance(state.get("snapshot_seq"), int)
            and not isinstance(state.get("snapshot_seq"), bool)
            and state.get("snapshot_seq") > 0
            else None
        ),
        "ts": ts,
        "state_hash": state_hash,
    }
    missing = [key for key, value in source.items() if value is None]
    return source, missing


def _legacy_identity_values(payload: Mapping[str, Any], source: str, field: str) -> List[Any]:
    aliases: Dict[str, Tuple[str, ...]] = {
        "session_id": ("session_id", "source_session_id"),
        "snapshot_id": ("snapshot_id", "source_snapshot_id"),
        "state_hash": ("state_hash", "source_state_hash"),
    }
    if source == "health":
        aliases = {
            "session_id": ("current_session_id", "session_id"),
            "snapshot_id": ("latest_snapshot_id", "snapshot_id"),
            "state_hash": ("latest_state_hash", "state_hash"),
        }
    values = [payload[key] for key in aliases[field] if key in payload and payload[key] is not None]

    child_key = {
        "advice": "items",
        "incidents": "items",
        "plan": "plan",
        "strategy": "objectives",
        "policy": "items",
        "health": "items",
    }[source]
    children = payload.get(child_key)
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, Mapping):
                continue
            values.extend(child[key] for key in aliases[field] if key in child and child[key] is not None)
    return values


def _declared_identity(payload: Mapping[str, Any], output: str) -> Dict[str, Any]:
    declared = payload.get("source")
    if isinstance(declared, Mapping):
        return {field: declared.get(field) for field in IDENTITY_FIELDS}
    # The existing strategy schema already binds these fields at top level.
    # Keep this compatibility path until the publisher adds the unified source
    # object.  Every other child must use that object now.
    if output == "strategy":
        return {field: payload.get(field) for field in IDENTITY_FIELDS}
    return {field: None for field in IDENTITY_FIELDS}


def _identity_checks(
    payloads: Mapping[str, Mapping[str, Any]], source_identity: Mapping[str, Any]
) -> Tuple[List[Dict[str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    checks: List[Dict[str, str]] = []
    mismatches: List[Tuple[str, str]] = []
    missing: List[Tuple[str, str]] = []
    for output in OUTPUT_SOURCES:
        entry: Dict[str, str] = {"source": output}
        declared = _declared_identity(payloads[output], output)
        has_explicit_source = isinstance(payloads[output].get("source"), Mapping)
        for field in IDENTITY_FIELDS:
            expected = source_identity.get(field)
            claim = declared.get(field)
            # Child items often retain the snapshot that originally opened an
            # incident.  Once a payload has an explicit generation-level
            # source, those provenance fields must not be mistaken for the
            # identity of the output bundle itself.
            legacy_values = (
                []
                if has_explicit_source
                else _legacy_identity_values(payloads[output], output, field)
            )
            if expected is None or claim is None:
                result = "missing"
                missing.append((output, field))
            elif claim != expected:
                result = "mismatch"
                mismatches.append((output, field))
            elif any(value != expected for value in legacy_values):
                result = "mismatch"
                mismatches.append((output, field))
            else:
                result = "match"
            entry[field] = result
        checks.append(entry)
    return checks, mismatches, missing


def _safe_pipeline_token(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _PIPELINE_TOKEN_RE.fullmatch(value):
        return None
    if _SENSITIVE_RE.search(value) or _PATH_RE.search(value):
        return None
    return value


def _pipeline_tokens(value: Any, *, limit: int = 256) -> Tuple[List[str], bool]:
    if not isinstance(value, list) or len(value) > limit:
        return [], False
    tokens: List[str] = []
    for item in value:
        token = _safe_pipeline_token(item)
        if token is None:
            return [], False
        tokens.append(token)
    if tokens != sorted(set(tokens)):
        return [], False
    return tokens, True


def _empty_command_pipeline_projection(schema: Optional[str]) -> Dict[str, Any]:
    return {
        "valid": False,
        "schema": schema,
        "ready": False,
        "blockers": [],
        "readiness": {
            "mutation_proposal": {"ready": False, "blockers": []},
            "mutation_confirmation": {"ready": False, "blockers": [], "candidate_ids": []},
        },
        "counts": {stage: 0 for stage in COMMAND_PIPELINE_STAGES},
        "in_flight_mutation_count": 0,
        "ambiguous_outcome_count": 0,
        "duplicate_id_count": 0,
        "stale_pending_count": 0,
        "invalid_record_count": 0,
        "proposal_frame_evaluated": False,
        "proposal_frame_binding": None,
    }


def _project_command_pipeline(value: Any) -> Dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    schema = COMMAND_PIPELINE_SCHEMA if raw.get("schema") == COMMAND_PIPELINE_SCHEMA else None
    invalid = _empty_command_pipeline_projection(schema)
    expected_keys = {
        "schema",
        "ready",
        "blockers",
        "readiness",
        "counts",
        "in_flight_mutation_count",
        "ambiguous_outcome_count",
        "duplicate_id_count",
        "stale_pending_count",
        "invalid_record_count",
        "proposal_frame_evaluated",
        "proposal_frame_binding",
    }
    if not isinstance(value, Mapping) or set(raw) != expected_keys:
        return invalid

    readiness = raw.get("readiness")
    counts = raw.get("counts")
    if (
        schema is None
        or not isinstance(raw.get("ready"), bool)
        or not isinstance(readiness, Mapping)
        or set(readiness) != {"mutation_proposal", "mutation_confirmation"}
        or not isinstance(counts, Mapping)
        or set(counts) != set(COMMAND_PIPELINE_STAGES)
        or raw.get("proposal_frame_evaluated") is not False
        or raw.get("proposal_frame_binding") != "deferred_until_sync_pending_commands"
    ):
        return invalid

    proposal = readiness.get("mutation_proposal")
    confirmation = readiness.get("mutation_confirmation")
    if (
        not isinstance(proposal, Mapping)
        or set(proposal) != {"ready", "blockers"}
        or not isinstance(confirmation, Mapping)
        or set(confirmation) != {"ready", "blockers", "candidate_ids"}
        or not isinstance(proposal.get("ready"), bool)
        or not isinstance(confirmation.get("ready"), bool)
    ):
        return invalid

    blockers, blockers_valid = _pipeline_tokens(raw.get("blockers"))
    proposal_blockers, proposal_blockers_valid = _pipeline_tokens(proposal.get("blockers"))
    confirmation_blockers, confirmation_blockers_valid = _pipeline_tokens(confirmation.get("blockers"))
    candidate_ids, candidate_ids_valid = _pipeline_tokens(confirmation.get("candidate_ids"))
    counters = (
        "in_flight_mutation_count",
        "ambiguous_outcome_count",
        "duplicate_id_count",
        "stale_pending_count",
        "invalid_record_count",
    )
    counts_valid = all(
        isinstance(counts.get(stage), int)
        and not isinstance(counts.get(stage), bool)
        and counts.get(stage) >= 0
        for stage in COMMAND_PIPELINE_STAGES
    )
    counters_valid = all(
        isinstance(raw.get(key), int)
        and not isinstance(raw.get(key), bool)
        and raw.get(key) >= 0
        for key in counters
    )
    consistent = (
        raw["ready"] is (not blockers)
        and blockers == proposal_blockers
        and proposal["ready"] is (not proposal_blockers)
        and confirmation["ready"] is (not confirmation_blockers)
        and set(confirmation_blockers).issubset(proposal_blockers)
    )
    if not (
        blockers_valid
        and proposal_blockers_valid
        and confirmation_blockers_valid
        and candidate_ids_valid
        and counts_valid
        and counters_valid
        and consistent
    ):
        return invalid

    return {
        "valid": True,
        "schema": COMMAND_PIPELINE_SCHEMA,
        "ready": raw["ready"],
        "blockers": blockers,
        "readiness": {
            "mutation_proposal": {
                "ready": proposal["ready"],
                "blockers": proposal_blockers,
            },
            "mutation_confirmation": {
                "ready": confirmation["ready"],
                "blockers": confirmation_blockers,
                "candidate_ids": candidate_ids,
            },
        },
        "counts": {stage: counts[stage] for stage in COMMAND_PIPELINE_STAGES},
        **{key: raw[key] for key in counters},
        "proposal_frame_evaluated": False,
        "proposal_frame_binding": "deferred_until_sync_pending_commands",
    }


def _project_health(value: Mapping[str, Any]) -> Dict[str, Any]:
    status = _safe_status(value.get("status"))
    trust_ready = value.get("trust_ready")
    replay_mode = value.get("replay_mode")
    session_start_observed = value.get("session_start_observed")
    valid = (
        value.get("schema") == "oni.bridge.health.v1"
        and status is not None
        and isinstance(trust_ready, bool)
        and isinstance(replay_mode, bool)
        and isinstance(session_start_observed, bool)
    )
    projected = {
        "valid": valid,
        "status": status,
        "snapshot_age_seconds": _finite_number(value.get("snapshot_age_seconds")),
        "trust_ready": trust_ready if isinstance(trust_ready, bool) else False,
        "replay_mode": replay_mode if isinstance(replay_mode, bool) else False,
        "session_start_observed": (
            session_start_observed if isinstance(session_start_observed, bool) else False
        ),
    }
    # Health documents predating the pipeline gate remain readable. Once Bridge
    # publishes the field, however, its complete contract is projected and
    # malformed data cannot silently fall back to the legacy path.
    if "command_pipeline" in value:
        projected["command_pipeline"] = _project_command_pipeline(value.get("command_pipeline"))
    return projected


def _project_advice(value: Mapping[str, Any]) -> Dict[str, Any]:
    raw_items = value.get("items")
    valid = value.get("schema") == "oni.advice.v2" and isinstance(raw_items, list)
    items: List[Dict[str, Any]] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, Mapping):
                valid = False
                continue
            incident_id = _safe_identifier(item.get("incident_id"))
            risk = item.get("risk") if item.get("risk") in RISKS else "unknown"
            incident_opened_at = item.get("incident_opened_at")
            if (
                not isinstance(incident_opened_at, str)
                or parse_snapshot_timestamp(incident_opened_at) is None
            ):
                incident_opened_at = None
                valid = False
            if incident_id is None:
                valid = False
                continue
            items.append(
                {
                    "incident_id": incident_id,
                    "risk": risk,
                    "incident_opened_at": incident_opened_at,
                }
            )
    items.sort(key=lambda item: item["incident_id"])
    count = len(items)
    return {"valid": valid, "count": count, "truncated": count > 20, "items": items[:20]}


def _project_incidents(
    value: Mapping[str, Any], state: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    raw_items = value.get("items")
    valid = value.get("schema") == "oni.incidents.v1" and isinstance(raw_items, list)
    items: List[Dict[str, Any]] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, Mapping):
                valid = False
                continue
            incident_id = _safe_identifier(item.get("incident_id"))
            status = _safe_status(item.get("status"))
            risk = item.get("risk") if item.get("risk") in RISKS else "unknown"
            if incident_id is None or status is None:
                valid = False
                continue
            telemetry_raw = item.get("telemetry_state")
            telemetry_state = _safe_status(telemetry_raw)
            session_raw = item.get("session_id")
            incident_session_id = _safe_session(session_raw)
            snapshot_raw = item.get("snapshot_id")
            incident_snapshot_id = _safe_session(snapshot_raw)
            projected_sequences: Dict[str, Optional[int]] = {}
            for sequence_key in (
                "first_seq",
                "last_seq",
                "first_snapshot_seq",
                "last_snapshot_seq",
            ):
                raw_sequence = item.get(sequence_key)
                projected_sequences[sequence_key] = (
                    raw_sequence
                    if isinstance(raw_sequence, int)
                    and not isinstance(raw_sequence, bool)
                    and raw_sequence >= 0
                    else None
                )
            samples_raw = item.get("consecutive_samples")
            consecutive_samples = (
                samples_raw
                if isinstance(samples_raw, int)
                and not isinstance(samples_raw, bool)
                and samples_raw >= 0
                else None
            )
            first_raw = item.get("first_seen")
            first_seen = (
                first_raw
                if isinstance(first_raw, str)
                and parse_snapshot_timestamp(first_raw) is not None
                else None
            )
            last_raw = item.get("last_seen")
            last_seen = (
                last_raw
                if isinstance(last_raw, str)
                and parse_snapshot_timestamp(last_raw) is not None
                else None
            )
            for raw, projected in (
                (telemetry_raw, telemetry_state),
                (session_raw, incident_session_id),
                (snapshot_raw, incident_snapshot_id),
                *(
                    (item.get(sequence_key), projected_sequences[sequence_key])
                    for sequence_key in (
                        "first_seq",
                        "last_seq",
                        "first_snapshot_seq",
                        "last_snapshot_seq",
                    )
                ),
                (samples_raw, consecutive_samples),
                (first_raw, first_seen),
                (last_raw, last_seen),
            ):
                if raw is not None and projected is None:
                    valid = False
            items.append(
                {
                    "incident_id": incident_id,
                    "status": status,
                    "risk": risk,
                    "telemetry_state": telemetry_state,
                    "session_id": incident_session_id,
                    "snapshot_id": incident_snapshot_id,
                    "first_seq": projected_sequences["first_seq"],
                    "last_seq": projected_sequences["last_seq"],
                    "first_snapshot_seq": projected_sequences["first_snapshot_seq"],
                    "last_snapshot_seq": projected_sequences["last_snapshot_seq"],
                    "consecutive_samples": consecutive_samples,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                }
            )
    items.sort(key=lambda item: item["incident_id"])
    count = len(items)
    projection: Dict[str, Any] = {
        "valid": valid,
        "count": count,
        "truncated": count > 100,
        "items": items[:100],
    }
    if isinstance(state, Mapping):
        from agent import rules as agent_rules

        source_items = value.get("items") if isinstance(value.get("items"), list) else []
        projection["survival_emergency"] = agent_rules.build_survival_arbitration_certificate(
            state,
            [item for item in source_items if isinstance(item, Mapping)],
        )
    return projection


def _project_plan(value: Mapping[str, Any]) -> Dict[str, Any]:
    raw_items = value.get("plan")
    mode = value.get("mode") if value.get("mode") in {"rule_only", "llm_ranked"} else None
    llm = value.get("llm") if isinstance(value.get("llm"), Mapping) else {}
    llm_status = _safe_status(llm.get("status"))
    valid = (
        value.get("schema") == "oni.plan.v2"
        and mode is not None
        and isinstance(raw_items, list)
        and isinstance(value.get("llm"), Mapping)
        and isinstance(llm.get("enabled"), bool)
        and isinstance(llm.get("called"), bool)
        and llm_status is not None
    )
    items: List[Dict[str, Any]] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, Mapping):
                valid = False
                continue
            incident_id = _safe_identifier(item.get("incident_id"))
            priority = item.get("priority")
            risk = item.get("risk") if item.get("risk") in RISKS else "unknown"
            if incident_id is None or not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
                valid = False
                continue
            items.append({"priority": priority, "incident_id": incident_id, "risk": risk})
    items.sort(key=lambda item: (item["priority"], item["incident_id"]))
    count = len(items)
    return {
        "valid": valid,
        "mode": mode,
        "count": count,
        "truncated": count > 8,
        "items": items[:8],
        "llm": {
            "enabled": llm.get("enabled") if isinstance(llm.get("enabled"), bool) else False,
            "called": llm.get("called") if isinstance(llm.get("called"), bool) else False,
            "status": llm_status,
        },
    }


def _strategy_recommendations(value: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    raw = value.get("next_actions")
    if not isinstance(raw, list):
        return [], False
    projected: List[Dict[str, Any]] = []
    valid = True
    for item in raw:
        if not isinstance(item, Mapping):
            valid = False
            continue
        identifier = _safe_identifier(item.get("id"))
        risk = item.get("risk") if item.get("risk") in RISKS else "unknown"
        non_executing = item.get("auto_execute") is False and item.get("requires_approval") is True
        if identifier is None or not non_executing:
            valid = False
            continue
        # No action, args, command, prose, or evidence is copied here.
        projected.append(
            {
                "id": identifier,
                "kind": "operator_recommendation",
                "risk": risk,
                "requires_approval": True,
                "auto_execute": False,
            }
        )
    projected.sort(key=lambda item: item["id"])
    if len(projected) > 8:
        valid = False
    return projected[:8], valid


def _project_strategy(value: Mapping[str, Any]) -> Dict[str, Any]:
    phase_value = value.get("phase") if isinstance(value.get("phase"), Mapping) else {}
    readiness_value = value.get("readiness") if isinstance(value.get("readiness"), Mapping) else {}
    phase_id = phase_value.get("id") if phase_value.get("id") in STRATEGY_PHASES else None
    confidence = (
        phase_value.get("confidence")
        if phase_value.get("confidence") in STRATEGY_CONFIDENCES
        else None
    )
    status = value.get("status") if value.get("status") in STRATEGY_STATUSES else None
    operating_mode = (
        value.get("operating_mode")
        if value.get("operating_mode") in STRATEGY_OPERATING_MODES
        else None
    )
    readiness_status = (
        readiness_value.get("status")
        if readiness_value.get("status") in STRATEGY_READINESS_STATUSES
        else None
    )
    stable_window_verified = readiness_value.get("stable_window_verified")
    required_samples = readiness_value.get("required_consecutive_samples")
    observed_samples = readiness_value.get("observed_consecutive_samples")
    required_duration_seconds = _finite_number(readiness_value.get("required_duration_seconds"))
    observed_duration_seconds = _finite_number(readiness_value.get("observed_duration_seconds"))
    required_game_cycles = _finite_number(readiness_value.get("required_game_cycles"))
    observed_game_cycles = _finite_number(readiness_value.get("observed_game_cycles"))
    thresholds_met = bool(
        isinstance(required_samples, int)
        and not isinstance(required_samples, bool)
        and isinstance(observed_samples, int)
        and not isinstance(observed_samples, bool)
        and observed_samples >= required_samples
        and required_duration_seconds is not None
        and observed_duration_seconds is not None
        and observed_duration_seconds >= required_duration_seconds
        and required_game_cycles is not None
        and observed_game_cycles is not None
        and observed_game_cycles >= required_game_cycles
    )
    phase_evidence = value.get("phase_evidence") if isinstance(value.get("phase_evidence"), Mapping) else {}
    evidence_window = (
        phase_evidence.get("evidence_window")
        if isinstance(phase_evidence.get("evidence_window"), Mapping)
        else {}
    )
    window_start = parse_timestamp(evidence_window.get("start_ts"))
    window_end = parse_timestamp(evidence_window.get("end_ts"))
    window_start_cycles = _finite_number(evidence_window.get("start_game_time_cycles"))
    window_end_cycles = _finite_number(evidence_window.get("end_game_time_cycles"))
    window_start_snapshot_seq = evidence_window.get("start_snapshot_seq")
    window_end_snapshot_seq = evidence_window.get("end_snapshot_seq")
    strategy_snapshot_seq = value.get("snapshot_seq")
    window_wall_seconds = (
        (window_end - window_start).total_seconds()
        if window_start is not None
        and window_end is not None
        and window_start.tzinfo is not None
        and window_end.tzinfo is not None
        and window_end >= window_start
        else None
    )
    window_game_cycles = (
        window_end_cycles - window_start_cycles
        if window_start_cycles is not None
        and window_end_cycles is not None
        and window_end_cycles >= window_start_cycles
        else None
    )
    evidence_window_valid = bool(
        phase_evidence.get("schema") == "oni.phase-evidence.v1"
        and phase_evidence.get("session_id") == value.get("session_id")
        and phase_evidence.get("last_snapshot_id") == value.get("snapshot_id")
        and phase_evidence.get("current_phase") == phase_id
        and evidence_window.get("schema") == "oni.evidence-window.v1"
        and evidence_window.get("phase") == phase_id
        and evidence_window.get("gap_free") is True
        and evidence_window.get("critical_incidents") == 0
        and isinstance(evidence_window.get("sample_count"), int)
        and not isinstance(evidence_window.get("sample_count"), bool)
        and evidence_window.get("sample_count") == observed_samples
        and isinstance(window_start_snapshot_seq, int)
        and not isinstance(window_start_snapshot_seq, bool)
        and window_start_snapshot_seq > 0
        and isinstance(window_end_snapshot_seq, int)
        and not isinstance(window_end_snapshot_seq, bool)
        and window_end_snapshot_seq >= window_start_snapshot_seq
        and window_end_snapshot_seq - window_start_snapshot_seq + 1 == observed_samples
        and isinstance(strategy_snapshot_seq, int)
        and not isinstance(strategy_snapshot_seq, bool)
        and strategy_snapshot_seq == window_end_snapshot_seq
        and isinstance(evidence_window.get("input_hash"), str)
        and _STATE_HASH_RE.fullmatch(str(evidence_window.get("input_hash"))) is not None
        and window_wall_seconds is not None
        and observed_duration_seconds is not None
        and math.isclose(window_wall_seconds, observed_duration_seconds, rel_tol=0.0, abs_tol=1e-6)
        and window_game_cycles is not None
        and observed_game_cycles is not None
        and math.isclose(window_game_cycles, observed_game_cycles, rel_tol=0.0, abs_tol=1e-6)
    )
    readiness_valid = (
        readiness_status is not None
        and isinstance(stable_window_verified, bool)
        and isinstance(required_samples, int)
        and not isinstance(required_samples, bool)
        and required_samples >= 0
        and isinstance(observed_samples, int)
        and not isinstance(observed_samples, bool)
        and observed_samples >= 0
        and required_duration_seconds is not None
        and observed_duration_seconds is not None
        and required_game_cycles is not None
        and observed_game_cycles is not None
        and (
            readiness_status != "ready"
            or (stable_window_verified is True and thresholds_met and evidence_window_valid)
        )
    )
    raw_objectives = value.get("objectives")
    valid = (
        value.get("schema") == "oni.strategy.v2"
        and value.get("mode") == "deterministic"
        and value.get("auto_execute") is False
        and status is not None
        and operating_mode is not None
        and phase_id is not None
        and confidence is not None
        and readiness_valid
        and isinstance(raw_objectives, list)
        and bool(raw_objectives)
    )
    objectives: List[Dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw_objectives, list):
        for item in raw_objectives:
            if not isinstance(item, Mapping):
                valid = False
                continue
            identifier = _safe_identifier(item.get("id"))
            item_phase = item.get("phase") if item.get("phase") in STRATEGY_PHASES else None
            item_status = item.get("status") if item.get("status") in OBJECTIVE_STATUSES else None
            priority = item.get("priority")
            if (
                identifier is None
                or identifier in seen
                or item_phase is None
                or item_status is None
                or not isinstance(priority, int)
                or isinstance(priority, bool)
            ):
                valid = False
                continue
            seen.add(identifier)
            objectives.append(
                {"id": identifier, "phase": item_phase, "status": item_status, "priority": priority}
            )
            recommendations = item.get("recommended_actions")
            if not isinstance(recommendations, list):
                valid = False
            else:
                for recommendation in recommendations:
                    if not isinstance(recommendation, Mapping) or recommendation.get("auto_execute") is not False or recommendation.get("requires_approval") is not True:
                        valid = False
    objectives.sort(key=lambda item: item["id"])
    if len(objectives) > 64:
        valid = False
        objectives = objectives[:64]
    objective_status = {item["id"]: item["status"] for item in objectives}
    recommendations, recommendations_valid = _strategy_recommendations(value)
    valid = valid and recommendations_valid
    gates = [
        {
            "id": gate_id,
            "status": objective_status.get(gate_id, "missing"),
            "advance_required": gate_id in ADVANCE_GATE_IDS,
            "sustainability_required": gate_id in SUSTAINABILITY_GATE_IDS,
        }
        for gate_id in ALL_GATE_IDS
    ]
    indefinite_status = objective_status.get("indefinite_survival", "missing")
    return {
        "valid": valid,
        "status": status,
        "operating_mode": operating_mode,
        "phase": phase_id,
        "confidence": confidence,
        "readiness": {
            "status": readiness_status if readiness_valid else None,
            "stable_window_verified": (
                stable_window_verified
                if readiness_valid and isinstance(stable_window_verified, bool)
                else False
            ),
            "required_consecutive_samples": (
                required_samples
                if isinstance(required_samples, int) and not isinstance(required_samples, bool) and required_samples >= 0
                else 0
            ),
            "observed_consecutive_samples": (
                observed_samples
                if isinstance(observed_samples, int) and not isinstance(observed_samples, bool) and observed_samples >= 0
                else 0
            ),
            "required_duration_seconds": required_duration_seconds or 0.0,
            "observed_duration_seconds": observed_duration_seconds or 0.0,
            "required_game_cycles": required_game_cycles or 0.0,
            "observed_game_cycles": observed_game_cycles or 0.0,
            "evidence_window_valid": evidence_window_valid if readiness_valid else False,
        },
        "objectives": objectives,
        "gates": gates,
        "indefinite_survival": indefinite_status,
        "recommendations": recommendations,
    }


def _project_policy(value: Mapping[str, Any]) -> Dict[str, Any]:
    # Bridge adds only copied source identity to the frame input. It is not
    # part of the on-disk policy document or its authorization hash.
    return project_action_policy(value, allow_synthetic_source=True)


def _add_blocker(
    blockers: List[Dict[str, Optional[str]]], code: str, source: str, field: Optional[str] = None
) -> None:
    item = {"code": code, "source": source, "field": field}
    if item not in blockers:
        blockers.append(item)


def _build_readiness(blockers: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    codes = {str(item.get("code")) for item in blockers}
    result: Dict[str, Dict[str, Any]] = {}
    for stage in READINESS_STAGES:
        stage_codes = sorted(codes & _STAGE_BLOCKERS[stage])
        result[stage] = {"ready": not stage_codes, "blockers": stage_codes}
    return result


def _frame_digest(frame: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in frame.items() if key != "frame_id"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_decision_frame(
    latest_state: Optional[Mapping[str, Any]],
    health: Optional[Mapping[str, Any]],
    advice_payload: Optional[Mapping[str, Any]],
    incidents_payload: Optional[Mapping[str, Any]],
    plan: Optional[Mapping[str, Any]],
    strategy: Optional[Mapping[str, Any]],
    policy: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build one deterministic, non-executing decision frame.

    Inputs are deep-copied before inspection and are never modified.  The
    result contains no references to caller-owned containers.  ``frame_id`` is
    the SHA-256 digest of the complete canonical payload other than the digest
    field itself, making post-build mutation detectable by
    :func:`validate_decision_frame`.
    """

    state_value = _mapping(latest_state)
    payloads: Dict[str, Dict[str, Any]] = {
        "health": _mapping(health),
        "advice": _mapping(advice_payload),
        "incidents": _mapping(incidents_payload),
        "plan": _mapping(plan),
        "strategy": _mapping(strategy),
        "policy": _mapping(policy),
    }
    source, missing_identity = _source_identity(state_value)
    state_valid = bool(
        state_value.get("schema") == "oni.state.v2"
        and state_value.get("event") == "state_snapshot"
        and not missing_identity
        and source["seq"] is not None
        and source["snapshot_id"] == f"{source['session_id']}:{source['seq']}"
        and isinstance(state_value.get("save_dirty"), bool)
    )

    projections = {
        "health": _project_health(payloads["health"]),
        "advice": _project_advice(payloads["advice"]),
        "incidents": _project_incidents(payloads["incidents"], state_value),
        "plan": _project_plan(payloads["plan"]),
        "strategy": _project_strategy(payloads["strategy"]),
        "policy": _project_policy(payloads["policy"]),
    }
    identity_checks, mismatches, missing_output_identity = _identity_checks(payloads, source)
    if mismatches:
        fields = ",".join(f"{output}.{field}" for output, field in sorted(mismatches))
        raise DecisionFrameError(f"cross-output source identity mismatch: {fields}")
    stale = bool(
        isinstance(state_value.get("bridge"), Mapping) and state_value["bridge"].get("stale") is True
    ) or projections["health"]["status"] == "stale"
    health_ok = bool(
        projections["health"]["valid"]
        and projections["health"]["status"] == "ok"
        and projections["health"]["trust_ready"]
        and projections["health"]["session_start_observed"]
    )

    blockers: List[Dict[str, Optional[str]]] = []
    if not state_valid:
        _add_blocker(blockers, "STATE_INVALID", "state", "schema")
    for field in missing_identity:
        _add_blocker(blockers, "SOURCE_IDENTITY_MISSING", "state", field)
    if stale:
        _add_blocker(blockers, "STATE_STALE", "state", "ts")
    if projections["health"]["replay_mode"]:
        _add_blocker(blockers, "REPLAY_MODE", "health", "replay_mode")
    if not projections["health"]["valid"]:
        _add_blocker(blockers, "HEALTH_INVALID", "health", "schema")
    elif not health_ok:
        _add_blocker(blockers, "HEALTH_NOT_OK", "health", "status")
    pipeline_projection = projections["health"].get("command_pipeline")
    if pipeline_projection is not None:
        if not pipeline_projection["valid"]:
            _add_blocker(
                blockers,
                "COMMAND_PIPELINE_INVALID",
                "health",
                "command_pipeline",
            )
        elif not pipeline_projection["readiness"]["mutation_proposal"]["ready"]:
            _add_blocker(
                blockers,
                "COMMAND_PIPELINE_MUTATION_BLOCKED",
                "health",
                "command_pipeline.readiness.mutation_proposal",
            )
    for output, field in missing_output_identity:
        _add_blocker(blockers, "OUTPUT_IDENTITY_MISSING", output, field)

    if not projections["advice"]["valid"]:
        _add_blocker(blockers, "ADVICE_INVALID", "advice", "schema")
    if not projections["incidents"]["valid"]:
        _add_blocker(blockers, "INCIDENTS_INVALID", "incidents", "schema")
    if not projections["plan"]["valid"]:
        _add_blocker(blockers, "PLAN_INVALID", "plan", "schema")
    if not projections["strategy"]["valid"]:
        _add_blocker(blockers, "STRATEGY_INVALID", "strategy", "schema")
    if not projections["policy"]["valid"]:
        _add_blocker(blockers, "POLICY_INVALID", "policy", "schema")
    else:
        if not projections["policy"]["suggestions_enabled"]:
            _add_blocker(blockers, "SUGGESTIONS_DISABLED", "policy", "suggestions_enabled")
        if not projections["policy"]["actions_enabled"]:
            _add_blocker(blockers, "ACTIONS_DISABLED", "policy", "actions_enabled")

    strategy_projection = projections["strategy"]
    if strategy_projection["valid"]:
        if strategy_projection["status"] != "ok":
            _add_blocker(blockers, "STRATEGY_STATUS_NOT_READY", "strategy", "status")
        confidence = strategy_projection["confidence"]
        if confidence == "low":
            _add_blocker(blockers, "STRATEGY_CONFIDENCE_LOW", "strategy", "confidence")
        elif confidence == "blocked":
            _add_blocker(blockers, "STRATEGY_CONFIDENCE_BLOCKED", "strategy", "confidence")
        elif confidence != "high":
            _add_blocker(blockers, "STRATEGY_CONFIDENCE_NOT_HIGH", "strategy", "confidence")
        if not strategy_projection["readiness"]["stable_window_verified"]:
            _add_blocker(
                blockers,
                "STRATEGY_WINDOW_NOT_VERIFIED",
                "strategy",
                "stable_window_verified",
            )

        for gate in strategy_projection["gates"]:
            if gate["advance_required"] and gate["status"] != "satisfied":
                _add_blocker(blockers, "STRATEGY_CRITICAL_GATE_NOT_PASSED", "strategy", gate["id"])
            if gate["sustainability_required"] and gate["status"] != "satisfied":
                _add_blocker(blockers, "SUSTAINABILITY_GATE_NOT_PASSED", "strategy", gate["id"])
        if strategy_projection["phase"] != "sustain":
            _add_blocker(blockers, "STRATEGY_PHASE_NOT_SUSTAIN", "strategy", "phase")
        if strategy_projection["indefinite_survival"] != "satisfied":
            _add_blocker(
                blockers, "INDEFINITE_SURVIVAL_NOT_PROVEN", "strategy", "indefinite_survival"
            )

    blockers.sort(key=lambda item: (str(item["code"]), str(item["source"]), str(item["field"] or "")))
    source_identity_complete = not missing_identity
    output_identity_complete = not missing_output_identity
    identity_complete = source_identity_complete and output_identity_complete
    cross_output_consistent = True
    trust = {
        "status": (
            "trusted"
            if state_valid and identity_complete and not stale and health_ok and cross_output_consistent
            else "blocked"
        ),
        "state_valid": state_valid,
        "identity_complete": identity_complete,
        "source_identity_complete": source_identity_complete,
        "output_identity_complete": output_identity_complete,
        "fresh": not stale,
        "health_ok": health_ok,
        "cross_output_consistent": cross_output_consistent,
        "identity_checks": identity_checks,
    }
    frame: Dict[str, Any] = {
        "schema": DECISION_FRAME_SCHEMA,
        "frame_id": "",
        "source": source,
        "trust": trust,
        "readiness": _build_readiness(blockers),
        "blockers": blockers,
        "projections": projections,
    }
    frame["frame_id"] = _frame_digest(frame)
    validate_decision_frame(frame)
    return frame


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DecisionFrameError(message)


def _exact_keys(value: Any, keys: Iterable[str], label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    expected = set(keys)
    actual = set(value)
    _require(actual == expected, f"{label} keys must be {sorted(expected)}")
    return value


def _validate_items(items: Any, keys: Iterable[str], label: str, limit: int) -> List[Mapping[str, Any]]:
    _require(isinstance(items, list) and len(items) <= limit, f"{label} must be a bounded list")
    result: List[Mapping[str, Any]] = []
    for index, item in enumerate(items):
        result.append(_exact_keys(item, keys, f"{label}[{index}]"))
    return result


def _validate_command_pipeline_projection(value: Any) -> Mapping[str, Any]:
    projection = _exact_keys(
        value,
        {
            "valid",
            "schema",
            "ready",
            "blockers",
            "readiness",
            "counts",
            "in_flight_mutation_count",
            "ambiguous_outcome_count",
            "duplicate_id_count",
            "stale_pending_count",
            "invalid_record_count",
            "proposal_frame_evaluated",
            "proposal_frame_binding",
        },
        "frame.projections.health.command_pipeline",
    )
    _require(isinstance(projection["valid"], bool), "command pipeline valid flag must be boolean")
    _require(
        projection["schema"] in {None, COMMAND_PIPELINE_SCHEMA},
        "command pipeline schema is invalid",
    )
    if not projection["valid"]:
        _require(
            projection == _empty_command_pipeline_projection(projection["schema"]),
            "invalid command pipeline projection must use fail-closed defaults",
        )
        return projection

    _require(projection["schema"] == COMMAND_PIPELINE_SCHEMA, "command pipeline schema is invalid")
    _require(isinstance(projection["ready"], bool), "command pipeline ready flag must be boolean")
    blockers, blockers_valid = _pipeline_tokens(projection["blockers"])
    _require(blockers_valid, "command pipeline blockers are invalid")

    readiness = _exact_keys(
        projection["readiness"],
        {"mutation_proposal", "mutation_confirmation"},
        "frame.projections.health.command_pipeline.readiness",
    )
    proposal = _exact_keys(
        readiness["mutation_proposal"],
        {"ready", "blockers"},
        "frame.projections.health.command_pipeline.readiness.mutation_proposal",
    )
    confirmation = _exact_keys(
        readiness["mutation_confirmation"],
        {"ready", "blockers", "candidate_ids"},
        "frame.projections.health.command_pipeline.readiness.mutation_confirmation",
    )
    _require(isinstance(proposal["ready"], bool), "pipeline proposal ready flag must be boolean")
    _require(isinstance(confirmation["ready"], bool), "pipeline confirmation ready flag must be boolean")
    proposal_blockers, proposal_blockers_valid = _pipeline_tokens(proposal["blockers"])
    confirmation_blockers, confirmation_blockers_valid = _pipeline_tokens(confirmation["blockers"])
    candidate_ids, candidate_ids_valid = _pipeline_tokens(confirmation["candidate_ids"])
    _require(proposal_blockers_valid, "pipeline proposal blockers are invalid")
    _require(confirmation_blockers_valid, "pipeline confirmation blockers are invalid")
    _require(candidate_ids_valid, "pipeline confirmation candidate ids are invalid")
    _require(blockers == proposal_blockers, "pipeline proposal blockers disagree with the top-level gate")
    _require(projection["ready"] is (not blockers), "command pipeline ready flag is inconsistent")
    _require(proposal["ready"] is (not proposal_blockers), "pipeline proposal ready flag is inconsistent")
    _require(
        confirmation["ready"] is (not confirmation_blockers),
        "pipeline confirmation ready flag is inconsistent",
    )
    _require(
        set(confirmation_blockers).issubset(proposal_blockers),
        "pipeline confirmation blockers must also block a new mutation proposal",
    )

    counts = _exact_keys(
        projection["counts"],
        COMMAND_PIPELINE_STAGES,
        "frame.projections.health.command_pipeline.counts",
    )
    for stage in COMMAND_PIPELINE_STAGES:
        _require(
            isinstance(counts[stage], int)
            and not isinstance(counts[stage], bool)
            and counts[stage] >= 0,
            f"command pipeline count {stage} is invalid",
        )
    for key in (
        "in_flight_mutation_count",
        "ambiguous_outcome_count",
        "duplicate_id_count",
        "stale_pending_count",
        "invalid_record_count",
    ):
        _require(
            isinstance(projection[key], int)
            and not isinstance(projection[key], bool)
            and projection[key] >= 0,
            f"command pipeline {key} is invalid",
        )
    _require(
        projection["proposal_frame_evaluated"] is False,
        "pre-frame command pipeline cannot claim frame evaluation",
    )
    _require(
        projection["proposal_frame_binding"] == "deferred_until_sync_pending_commands",
        "command pipeline frame-binding handoff is invalid",
    )
    return projection


def validate_decision_frame(frame: Mapping[str, Any]) -> None:
    """Strictly validate schema, tamper hash, and fail-closed semantics."""

    top = _exact_keys(
        frame,
        {"schema", "frame_id", "source", "trust", "readiness", "blockers", "projections"},
        "frame",
    )
    _require(top.get("schema") == DECISION_FRAME_SCHEMA, f"frame.schema must be {DECISION_FRAME_SCHEMA}")
    _require(isinstance(top.get("frame_id"), str) and _STATE_HASH_RE.fullmatch(str(top["frame_id"])) is not None, "frame.frame_id is invalid")
    try:
        expected_digest = _frame_digest(top)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DecisionFrameError("frame must be canonical JSON") from exc
    _require(top["frame_id"] == expected_digest, "frame.frame_id does not match the payload")

    source = _exact_keys(
        top.get("source"),
        {"session_id", "save_id_hash", "world_id", "snapshot_id", "seq", "snapshot_seq", "ts", "state_hash"},
        "frame.source",
    )
    _require(source["session_id"] is None or _safe_session(source["session_id"]) == source["session_id"], "frame.source.session_id is invalid")
    _require(source["save_id_hash"] is None or (isinstance(source["save_id_hash"], str) and _SAVE_HASH_RE.fullmatch(source["save_id_hash"]) is not None), "frame.source.save_id_hash is invalid")
    _require(source["world_id"] is None or _safe_world(source["world_id"]) == source["world_id"], "frame.source.world_id is invalid")
    _require(source["snapshot_id"] is None or _safe_session(source["snapshot_id"]) == source["snapshot_id"], "frame.source.snapshot_id is invalid")
    _require(source["seq"] is None or (isinstance(source["seq"], int) and not isinstance(source["seq"], bool) and source["seq"] > 0), "frame.source.seq is invalid")
    _require(source["snapshot_seq"] is None or (isinstance(source["snapshot_seq"], int) and not isinstance(source["snapshot_seq"], bool) and source["snapshot_seq"] > 0), "frame.source.snapshot_seq is invalid")
    _require(
        source["snapshot_seq"] is None
        or source["seq"] is None
        or source["snapshot_seq"] <= source["seq"],
        "frame.source.snapshot_seq exceeds seq",
    )
    _require(
        source["ts"] is None
        or (
            isinstance(source["ts"], str)
            and parse_snapshot_timestamp(source["ts"]) is not None
        ),
        "frame.source.ts is invalid",
    )
    _require(source["state_hash"] is None or (isinstance(source["state_hash"], str) and _STATE_HASH_RE.fullmatch(source["state_hash"]) is not None), "frame.source.state_hash is invalid")

    trust = _exact_keys(
        top.get("trust"),
        {
            "status",
            "state_valid",
            "identity_complete",
            "source_identity_complete",
            "output_identity_complete",
            "fresh",
            "health_ok",
            "cross_output_consistent",
            "identity_checks",
        },
        "frame.trust",
    )
    _require(trust["status"] in {"trusted", "blocked"}, "frame.trust.status is invalid")
    for key in (
        "state_valid",
        "identity_complete",
        "source_identity_complete",
        "output_identity_complete",
        "fresh",
        "health_ok",
        "cross_output_consistent",
    ):
        _require(isinstance(trust[key], bool), f"frame.trust.{key} must be boolean")
    checks = _validate_items(
        trust["identity_checks"],
        {"source", "session_id", "snapshot_id", "state_hash"},
        "frame.trust.identity_checks",
        len(OUTPUT_SOURCES),
    )
    _require(len(checks) == len(OUTPUT_SOURCES), "frame.trust.identity_checks is incomplete")
    _require([item["source"] for item in checks] == list(OUTPUT_SOURCES), "identity checks are not in canonical source order")
    for item in checks:
        for field in IDENTITY_FIELDS:
            _require(item[field] in {"match", "mismatch", "missing"}, f"identity check {field} is invalid")
    mismatch_present = any(item[field] == "mismatch" for item in checks for field in IDENTITY_FIELDS)
    missing_output_identity = any(item[field] == "missing" for item in checks for field in IDENTITY_FIELDS)
    _require(not mismatch_present, "cross-output source identity mismatch")
    _require(trust["cross_output_consistent"] is True, "cross-output consistency flag is wrong")
    source_complete = all(source[key] is not None for key in source)
    output_complete = not missing_output_identity
    _require(trust["source_identity_complete"] is source_complete, "source identity completeness flag is wrong")
    _require(trust["output_identity_complete"] is output_complete, "output identity completeness flag is wrong")
    _require(trust["identity_complete"] is (source_complete and output_complete), "identity completeness flag is wrong")
    expected_trusted = bool(
        trust["state_valid"]
        and source_complete
        and output_complete
        and trust["fresh"]
        and trust["health_ok"]
    )
    _require((trust["status"] == "trusted") is expected_trusted, "trust status is not fail-closed")

    blockers_value = top.get("blockers")
    _require(isinstance(blockers_value, list), "frame.blockers must be a list")
    blocker_keys: List[Tuple[str, str, str]] = []
    for index, blocker in enumerate(blockers_value):
        item = _exact_keys(blocker, {"code", "source", "field"}, f"frame.blockers[{index}]")
        _require(item["code"] in BLOCKER_CODES, "unknown blocker code")
        _require(item["source"] in {"state", *OUTPUT_SOURCES}, "unknown blocker source")
        _require(item["field"] is None or _safe_identifier(item["field"]) == item["field"], "invalid blocker field")
        blocker_keys.append((str(item["code"]), str(item["source"]), str(item["field"] or "")))
    _require(blocker_keys == sorted(set(blocker_keys)), "frame.blockers must be unique and sorted")
    blocker_codes = {item[0] for item in blocker_keys}
    _require("OUTPUT_IDENTITY_MISMATCH" not in blocker_codes, "a mismatched frame must be rejected, not published")
    _require(("OUTPUT_IDENTITY_MISSING" in blocker_codes) is missing_output_identity, "missing output identity blocker is wrong")
    _require(("SOURCE_IDENTITY_MISSING" in blocker_codes) is (not source_complete), "missing source identity blocker is wrong")

    readiness = _exact_keys(top.get("readiness"), READINESS_STAGES, "frame.readiness")
    expected_readiness = _build_readiness(blockers_value)
    for stage in READINESS_STAGES:
        entry = _exact_keys(readiness[stage], {"ready", "blockers"}, f"frame.readiness.{stage}")
        _require(isinstance(entry["ready"], bool), f"frame.readiness.{stage}.ready must be boolean")
        _require(entry == expected_readiness[stage], f"frame.readiness.{stage} is inconsistent")
    if missing_output_identity:
        _require(all(readiness[stage]["ready"] is False for stage in READINESS_STAGES), "missing child identity must fail closed at every readiness stage")

    projections = _exact_keys(top.get("projections"), OUTPUT_SOURCES, "frame.projections")
    health_projection = projections["health"]
    _require(isinstance(health_projection, Mapping), "frame.projections.health must be an object")
    base_health_keys = {
        "valid",
        "status",
        "snapshot_age_seconds",
        "trust_ready",
        "replay_mode",
        "session_start_observed",
    }
    _require(
        frozenset(health_projection)
        in {frozenset(base_health_keys), frozenset(base_health_keys | {"command_pipeline"})},
        f"frame.projections.health keys must be {sorted(base_health_keys)} with optional command_pipeline",
    )
    pipeline_projection = (
        _validate_command_pipeline_projection(health_projection["command_pipeline"])
        if "command_pipeline" in health_projection
        else None
    )
    _require(isinstance(health_projection["valid"], bool), "health.valid must be boolean")
    for key in ("trust_ready", "replay_mode", "session_start_observed"):
        _require(isinstance(health_projection[key], bool), f"health.{key} must be boolean")
    _require(health_projection["status"] is None or _safe_status(health_projection["status"]) == health_projection["status"], "health.status is invalid")
    _require(health_projection["snapshot_age_seconds"] is None or _finite_number(health_projection["snapshot_age_seconds"]) is not None, "health snapshot age is invalid")
    _require(
        trust["health_ok"]
        is bool(
            health_projection["valid"]
            and health_projection["status"] == "ok"
            and health_projection["trust_ready"]
            and health_projection["session_start_observed"]
        ),
        "health trust flag is wrong",
    )

    advice_projection = _exact_keys(projections["advice"], {"valid", "count", "truncated", "items"}, "frame.projections.advice")
    incidents_raw = projections["incidents"]
    _require(isinstance(incidents_raw, Mapping), "frame.projections.incidents must be an object")
    incident_base_keys = {"valid", "count", "truncated", "items"}
    _require(
        frozenset(incidents_raw)
        in {
            frozenset(incident_base_keys),
            frozenset(incident_base_keys | {"survival_emergency"}),
        },
        "frame.projections.incidents keys are invalid",
    )
    incidents_projection = dict(incidents_raw)
    for label, projection, limit, item_keys in (
        (
            "advice",
            advice_projection,
            20,
            {"incident_id", "risk", "incident_opened_at"},
        ),
        ("incidents", incidents_projection, 100, INCIDENT_PROJECTION_KEYS),
    ):
        _require(isinstance(projection["valid"], bool), f"{label}.valid must be boolean")
        _require(isinstance(projection["count"], int) and projection["count"] >= 0, f"{label}.count is invalid")
        _require(isinstance(projection["truncated"], bool), f"{label}.truncated must be boolean")
        items = _validate_items(projection["items"], item_keys, f"frame.projections.{label}.items", limit)
        _require(projection["count"] >= len(items), f"{label}.count is inconsistent")
        _require(projection["truncated"] is (projection["count"] > limit), f"{label}.truncated is inconsistent")
        for item in items:
            _require(_safe_identifier(item["incident_id"]) == item["incident_id"], f"{label} incident id is invalid")
            _require(item["risk"] in RISKS, f"{label} risk is invalid")
            if label == "advice":
                opened_at = item["incident_opened_at"]
                opened_at_value = parse_snapshot_timestamp(opened_at)
                source_ts_value = parse_snapshot_timestamp(source["ts"])
                _require(
                    opened_at is None
                    or (isinstance(opened_at, str) and opened_at_value is not None),
                    "advice incident_opened_at is invalid",
                )
                _require(
                    advice_projection["valid"] is False or opened_at is not None,
                    "valid advice must bind incident_opened_at",
                )
                _require(
                    advice_projection["valid"] is False
                    or (
                        opened_at_value is not None
                        and source_ts_value is not None
                        and opened_at_value <= source_ts_value
                    ),
                    "advice incident_opened_at is after the source snapshot",
                )
            else:
                _require(_safe_status(item["status"]) == item["status"], "incident status is invalid")
                _require(
                    item["telemetry_state"] is None
                    or _safe_status(item["telemetry_state"]) == item["telemetry_state"],
                    "incident telemetry state is invalid",
                )
                for key in ("session_id", "snapshot_id"):
                    _require(
                        item[key] is None or _safe_session(item[key]) == item[key],
                        f"incident {key} is invalid",
                    )
                for key in (
                    "first_seq",
                    "last_seq",
                    "first_snapshot_seq",
                    "last_snapshot_seq",
                    "consecutive_samples",
                ):
                    _require(
                        item[key] is None
                        or (
                            isinstance(item[key], int)
                            and not isinstance(item[key], bool)
                            and item[key] >= 0
                        ),
                        f"incident {key} is invalid",
                    )
                for key in ("first_seen", "last_seen"):
                    _require(
                        item[key] is None
                        or (
                            isinstance(item[key], str)
                            and parse_snapshot_timestamp(item[key]) is not None
                        ),
                        f"incident {key} is invalid",
                    )
                if item["status"] in {"open", "pending", "cooldown"}:
                    _require(
                        (item["first_seen"] is None) is (item["last_seen"] is None),
                        "incident witness timestamps are incomplete",
                    )
    if "survival_emergency" in incidents_projection:
        from agent import rules as agent_rules

        certificate_blockers = agent_rules.survival_certificate_blockers(
            incidents_projection["survival_emergency"],
            source,
            incidents_projection["items"],
        )
        _require(
            not certificate_blockers,
            "survival emergency certificate is invalid: " + ",".join(certificate_blockers),
        )

    plan_projection = _exact_keys(projections["plan"], {"valid", "mode", "count", "truncated", "items", "llm"}, "frame.projections.plan")
    _require(isinstance(plan_projection["valid"], bool), "plan.valid must be boolean")
    _require(plan_projection["mode"] in {None, "rule_only", "llm_ranked"}, "plan.mode is invalid")
    _require(isinstance(plan_projection["count"], int) and plan_projection["count"] >= 0, "plan.count is invalid")
    _require(isinstance(plan_projection["truncated"], bool) and plan_projection["truncated"] is (plan_projection["count"] > 8), "plan.truncated is invalid")
    plan_items = _validate_items(plan_projection["items"], {"priority", "incident_id", "risk"}, "frame.projections.plan.items", 8)
    _require(plan_projection["count"] >= len(plan_items), "plan.count is inconsistent")
    for item in plan_items:
        _require(isinstance(item["priority"], int) and not isinstance(item["priority"], bool) and item["priority"] >= 1, "plan priority is invalid")
        _require(_safe_identifier(item["incident_id"]) == item["incident_id"] and item["risk"] in RISKS, "plan item is invalid")
    llm = _exact_keys(plan_projection["llm"], {"enabled", "called", "status"}, "frame.projections.plan.llm")
    _require(isinstance(llm["enabled"], bool) and isinstance(llm["called"], bool), "plan LLM flags must be boolean")
    _require(llm["status"] is None or _safe_status(llm["status"]) == llm["status"], "plan LLM status is invalid")

    strategy_projection = _exact_keys(
        projections["strategy"],
        {
            "valid",
            "status",
            "operating_mode",
            "phase",
            "confidence",
            "readiness",
            "objectives",
            "gates",
            "indefinite_survival",
            "recommendations",
        },
        "frame.projections.strategy",
    )
    _require(isinstance(strategy_projection["valid"], bool), "strategy.valid must be boolean")
    _require(strategy_projection["status"] in STRATEGY_STATUSES | {None}, "strategy status is invalid")
    _require(
        strategy_projection["operating_mode"] in STRATEGY_OPERATING_MODES | {None},
        "strategy operating mode is invalid",
    )
    _require(strategy_projection["phase"] in STRATEGY_PHASES | {None}, "strategy phase is invalid")
    _require(strategy_projection["confidence"] in STRATEGY_CONFIDENCES | {None}, "strategy confidence is invalid")
    strategy_readiness = _exact_keys(
        strategy_projection["readiness"],
        {
            "status",
            "stable_window_verified",
            "required_consecutive_samples",
            "observed_consecutive_samples",
            "required_duration_seconds",
            "observed_duration_seconds",
            "required_game_cycles",
            "observed_game_cycles",
            "evidence_window_valid",
        },
        "frame.projections.strategy.readiness",
    )
    _require(
        strategy_readiness["status"] in STRATEGY_READINESS_STATUSES | {None},
        "strategy readiness status is invalid",
    )
    _require(
        isinstance(strategy_readiness["stable_window_verified"], bool),
        "strategy stable-window flag must be boolean",
    )
    for key in ("required_consecutive_samples", "observed_consecutive_samples"):
        _require(
            isinstance(strategy_readiness[key], int)
            and not isinstance(strategy_readiness[key], bool)
            and strategy_readiness[key] >= 0,
            f"strategy readiness {key} is invalid",
        )
    for key in (
        "required_duration_seconds",
        "observed_duration_seconds",
        "required_game_cycles",
        "observed_game_cycles",
    ):
        _require(
            _finite_number(strategy_readiness[key]) is not None,
            f"strategy readiness {key} is invalid",
        )
    _require(
        isinstance(strategy_readiness["evidence_window_valid"], bool),
        "strategy evidence-window flag must be boolean",
    )
    _require(
        strategy_readiness["status"] != "ready"
        or (
            strategy_readiness["stable_window_verified"] is True
            and strategy_readiness["evidence_window_valid"] is True
            and strategy_readiness["observed_consecutive_samples"]
            >= strategy_readiness["required_consecutive_samples"]
            and strategy_readiness["observed_duration_seconds"]
            >= strategy_readiness["required_duration_seconds"]
            and strategy_readiness["observed_game_cycles"]
            >= strategy_readiness["required_game_cycles"]
        ),
        "strategy readiness cannot be ready without a verified evidence window",
    )
    objectives = _validate_items(strategy_projection["objectives"], {"id", "phase", "status", "priority"}, "frame.projections.strategy.objectives", 64)
    seen_objectives: set[str] = set()
    for item in objectives:
        _require(_safe_identifier(item["id"]) == item["id"] and item["id"] not in seen_objectives, "strategy objective id is invalid")
        seen_objectives.add(item["id"])
        _require(item["phase"] in STRATEGY_PHASES and item["status"] in OBJECTIVE_STATUSES, "strategy objective phase/status is invalid")
        _require(isinstance(item["priority"], int) and not isinstance(item["priority"], bool), "strategy objective priority is invalid")
    gates = _validate_items(strategy_projection["gates"], {"id", "status", "advance_required", "sustainability_required"}, "frame.projections.strategy.gates", len(ALL_GATE_IDS))
    _require([item["id"] for item in gates] == list(ALL_GATE_IDS), "strategy gates are incomplete or unordered")
    for item in gates:
        _require(item["status"] in OBJECTIVE_STATUSES | {"missing"}, "strategy gate status is invalid")
        _require(item["advance_required"] is (item["id"] in ADVANCE_GATE_IDS), "advance gate flag is wrong")
        _require(item["sustainability_required"] is (item["id"] in SUSTAINABILITY_GATE_IDS), "sustainability gate flag is wrong")
    _require(strategy_projection["indefinite_survival"] in OBJECTIVE_STATUSES | {"missing"}, "indefinite survival status is invalid")
    recommendations = _validate_items(strategy_projection["recommendations"], {"id", "kind", "risk", "requires_approval", "auto_execute"}, "frame.projections.strategy.recommendations", 8)
    for item in recommendations:
        _require(_safe_identifier(item["id"]) == item["id"], "strategy recommendation id is invalid")
        _require(item["kind"] == "operator_recommendation" and item["risk"] in RISKS, "strategy recommendation type/risk is invalid")
        _require(item["requires_approval"] is True and item["auto_execute"] is False, "strategy recommendation became executable")

    policy_projection = _exact_keys(
        projections["policy"],
        {
            "valid",
            "policy_hash",
            "actions_enabled",
            "suggestions_enabled",
            "allowed_actions_count",
            "allowed_save_hash_count",
        },
        "frame.projections.policy",
    )
    for key in ("valid", "actions_enabled", "suggestions_enabled"):
        _require(isinstance(policy_projection[key], bool), f"policy.{key} must be boolean")
    _require(
        (
            policy_projection["valid"] is True
            and isinstance(policy_projection["policy_hash"], str)
            and _STATE_HASH_RE.fullmatch(policy_projection["policy_hash"]) is not None
        )
        or (policy_projection["valid"] is False and policy_projection["policy_hash"] is None),
        "policy.policy_hash must exactly identify a valid canonical policy",
    )
    for key in ("allowed_actions_count", "allowed_save_hash_count"):
        _require(isinstance(policy_projection[key], int) and policy_projection[key] >= 0, f"policy.{key} is invalid")

    # Semantic blocker checks make a hand-crafted or re-hashed optimistic frame
    # invalid even when its basic JSON shape is correct.
    required_codes: set[str] = set()
    if not trust["state_valid"]:
        required_codes.add("STATE_INVALID")
    if not source_complete:
        required_codes.add("SOURCE_IDENTITY_MISSING")
    if missing_output_identity:
        required_codes.add("OUTPUT_IDENTITY_MISSING")
    if not trust["fresh"]:
        required_codes.add("STATE_STALE")
    if not health_projection["valid"]:
        required_codes.add("HEALTH_INVALID")
    elif not (
        health_projection["status"] == "ok"
        and health_projection["trust_ready"]
        and health_projection["session_start_observed"]
    ):
        required_codes.add("HEALTH_NOT_OK")
    if health_projection["replay_mode"]:
        required_codes.add("REPLAY_MODE")
    expected_pipeline_blockers: set[Tuple[str, str, str]] = set()
    if pipeline_projection is not None:
        if not pipeline_projection["valid"]:
            required_codes.add("COMMAND_PIPELINE_INVALID")
            expected_pipeline_blockers.add(
                ("COMMAND_PIPELINE_INVALID", "health", "command_pipeline")
            )
        elif not pipeline_projection["readiness"]["mutation_proposal"]["ready"]:
            required_codes.add("COMMAND_PIPELINE_MUTATION_BLOCKED")
            expected_pipeline_blockers.add(
                (
                    "COMMAND_PIPELINE_MUTATION_BLOCKED",
                    "health",
                    "command_pipeline.readiness.mutation_proposal",
                )
            )
    for valid, code in (
        (advice_projection["valid"], "ADVICE_INVALID"),
        (incidents_projection["valid"], "INCIDENTS_INVALID"),
        (plan_projection["valid"], "PLAN_INVALID"),
        (strategy_projection["valid"], "STRATEGY_INVALID"),
        (policy_projection["valid"], "POLICY_INVALID"),
    ):
        if not valid:
            required_codes.add(code)
    if policy_projection["valid"]:
        if not policy_projection["suggestions_enabled"]:
            required_codes.add("SUGGESTIONS_DISABLED")
        if not policy_projection["actions_enabled"]:
            required_codes.add("ACTIONS_DISABLED")
    if strategy_projection["valid"]:
        if strategy_projection["status"] != "ok":
            required_codes.add("STRATEGY_STATUS_NOT_READY")
        confidence = strategy_projection["confidence"]
        if confidence == "low":
            required_codes.add("STRATEGY_CONFIDENCE_LOW")
        elif confidence == "blocked":
            required_codes.add("STRATEGY_CONFIDENCE_BLOCKED")
        elif confidence != "high":
            required_codes.add("STRATEGY_CONFIDENCE_NOT_HIGH")
        if not strategy_readiness["stable_window_verified"]:
            required_codes.add("STRATEGY_WINDOW_NOT_VERIFIED")
        if any(item["advance_required"] and item["status"] != "satisfied" for item in gates):
            required_codes.add("STRATEGY_CRITICAL_GATE_NOT_PASSED")
        if any(item["sustainability_required"] and item["status"] != "satisfied" for item in gates):
            required_codes.add("SUSTAINABILITY_GATE_NOT_PASSED")
        if strategy_projection["phase"] != "sustain":
            required_codes.add("STRATEGY_PHASE_NOT_SUSTAIN")
        if strategy_projection["indefinite_survival"] != "satisfied":
            required_codes.add("INDEFINITE_SURVIVAL_NOT_PROVEN")
    _require(required_codes <= blocker_codes, "frame is missing a required fail-closed blocker")
    actual_pipeline_blockers = {
        item
        for item in blocker_keys
        if item[0] in {"COMMAND_PIPELINE_INVALID", "COMMAND_PIPELINE_MUTATION_BLOCKED"}
    }
    _require(
        actual_pipeline_blockers == expected_pipeline_blockers,
        "command pipeline blocker projection is inconsistent",
    )

    serialized = json.dumps(top, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _require(_SENSITIVE_RE.search(serialized) is None, "frame contains credential-shaped text")
    _require(_PATH_RE.search(serialized) is None, "frame contains a local path")


__all__ = [
    "ADVANCE_GATE_IDS",
    "BLOCKER_CODES",
    "DECISION_FRAME_SCHEMA",
    "DecisionFrameError",
    "READINESS_STAGES",
    "SUSTAINABILITY_GATE_IDS",
    "build_decision_frame",
    "validate_decision_frame",
]
