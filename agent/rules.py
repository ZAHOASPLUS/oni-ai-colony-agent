from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    CANONICAL_ALERT_IDS,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    FUNCTIONAL_BUILDING_PREFAB_IDS,
    FUNCTIONAL_BUILDINGS_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    build_condition,
    read_contract_field,
)
from agent.canonical_hash import canonical_hash
from agent.incidents import IncidentTracker
from agent.state_quality import parse_snapshot_timestamp


AVAILABLE_ACTIONS: List[Dict[str, Any]] = [
    {
        "action": "noop",
        "risk": "low",
        "description": "Acknowledge a command without changing game state.",
    },
    {
        "action": "focus_note",
        "risk": "low",
        "description": "Write a local focus marker to the probe event log.",
    },
    {
        "action": "set_paused",
        "risk": "low",
        "description": "Set the game pause state and verify the resulting state.",
    },
    {
        "action": "set_research",
        "risk": "low",
        "description": "Set active research on an approved test save and verify the research id.",
    },
]


# Closed policy over every Probe canonical alert.  Ranking is intentionally a
# total order only for pause-eligible hazards; observe-only alerts may inform
# strategy but never acquire mutation authority here.
SURVIVAL_ALERT_POLICY: Dict[str, Dict[str, Any]] = {
    "suffocation": {
        "disposition": "pause", "rank": 0, "title": "Suffocation alert observed",
        "risk": "high", "action": "Pause and inspect the affected duplicant immediately.",
        "reason": "The canonical suffocation alert is active.", "samples": 1, "duration": 0.0, "cooldown": 0.0,
    },
    "incapacitated": {
        "disposition": "pause", "rank": 1, "title": "Incapacitated duplicant alert observed",
        "risk": "high", "action": "Pause and inspect rescue and treatment paths immediately.",
        "reason": "The canonical incapacitated alert is active.", "samples": 1, "duration": 0.0, "cooldown": 0.0,
    },
    "starvation": {
        "disposition": "pause", "rank": 2, "title": "Starvation alert observed",
        "risk": "high", "action": "Pause and inspect reachable permitted food immediately.",
        "reason": "The canonical starvation alert is active.", "samples": 1, "duration": 0.0, "cooldown": 0.0,
    },
    "bionic_offline": {
        "disposition": "pause", "rank": 3, "title": "Bionic duplicant offline alert observed",
        "risk": "high", "action": "Pause and inspect the bionic duplicant and power path immediately.",
        "reason": "The canonical bionic_offline alert is active.", "samples": 1, "duration": 0.0, "cooldown": 0.0,
    },
    "hyperthermia": {
        "disposition": "pause", "rank": 4, "title": "Hyperthermia alert observed",
        "risk": "high", "action": "Pause and inspect heat exposure and a safe route immediately.",
        "reason": "The canonical hyperthermia alert is active.", "samples": 1, "duration": 0.0, "cooldown": 0.0,
    },
    "hypothermia": {
        "disposition": "pause", "rank": 5, "title": "Hypothermia alert observed",
        "risk": "high", "action": "Pause and inspect cold exposure and a safe route immediately.",
        "reason": "The canonical hypothermia alert is active.", "samples": 1, "duration": 0.0, "cooldown": 0.0,
    },
    "major_sickness": {
        "disposition": "pause", "rank": 6, "title": "Major sickness alert observed",
        "risk": "high", "action": "Pause and inspect treatment and exposure immediately.",
        "reason": "The canonical major_sickness alert is active.", "samples": 1, "duration": 0.0, "cooldown": 0.0,
    },
    "low_oxygen": {
        "disposition": "pause", "rank": 7, "title": "Oxygen alert observed",
        "risk": "high", "action": "Inspect oxygen production and diffusion before nonessential work.",
        "reason": "The canonical low_oxygen alert persisted across the configured confirmation window.",
        "samples": 7, "duration": 30.0, "cooldown": 60.0,
    },
    "bionic_low_battery": {
        "disposition": "pause", "rank": 8, "title": "Bionic low-battery alert observed",
        "risk": "high", "action": "Pause and inspect a reachable safe recharge path.",
        "reason": "The canonical bionic_low_battery alert persisted across its confirmation window.",
        "samples": 2, "duration": 5.0, "cooldown": 60.0,
    },
    "exhaustion": {
        "disposition": "observe", "rank": None, "title": "Exhaustion alert observed",
        "risk": "medium", "action": "Inspect sleep access and schedules before assigning more work.",
        "reason": "The canonical exhaustion alert is active.", "samples": 1, "duration": 0.0, "cooldown": 60.0,
    },
    "high_stress": {
        "disposition": "observe", "rank": None, "title": "High-stress alert observed",
        "risk": "medium", "action": "Inspect morale, downtime, and stress reactions.",
        "reason": "The canonical high_stress alert is active.", "samples": 1, "duration": 0.0, "cooldown": 60.0,
    },
    "sickness": {
        "disposition": "observe", "rank": None, "title": "Sickness alert observed",
        "risk": "medium", "action": "Inspect disease exposure and available treatment.",
        "reason": "The canonical sickness alert is active.", "samples": 1, "duration": 0.0, "cooldown": 60.0,
    },
    "urgent_toilet": {
        "disposition": "observe", "rank": None, "title": "Urgent toilet alert observed",
        "risk": "medium", "action": "Inspect toilet availability, access, and queueing.",
        "reason": "The canonical urgent_toilet alert is active.", "samples": 1, "duration": 0.0, "cooldown": 60.0,
    },
    "wounded": {
        "disposition": "observe", "rank": None, "title": "Wounded duplicant alert observed",
        "risk": "medium", "action": "Inspect injury severity and treatment; this alert is not severity-specific.",
        "reason": "The canonical wounded alert is active, but its severity is not sufficiently specific for mutation.",
        "samples": 1, "duration": 0.0, "cooldown": 60.0,
    },
}

_ALERT_POLICY_KEYS = {
    "disposition", "rank", "title", "risk", "action", "reason", "samples", "duration", "cooldown"
}

# ``evaluate_signals`` also emits these retained rule observations.  They are
# valid incident identities, but never belong to the survival pause domain.
#
# The first group is present in every state snapshot.  The functional-building
# group is additive: it is emitted only when the optional
# ``functional_buildings`` field is present.  Keeping both groups here makes
# the emitted catalog explicit without making older snapshots fail closed just
# because they predate that additive telemetry.
BASE_ORDINARY_RULE_INCIDENT_IDS = (
    "idle_duplicants",
    "low_food",
    "missing_manual_generator",
    "missing_outhouse",
    "missing_oxygen_diffuser",
    "no_active_research",
    "stressed_duplicants",
)

FUNCTIONAL_BUILDING_DIAGNOSTICS = (
    ("OxygenDiffuser", "non_operational_oxygen_diffuser", "oxygen diffuser"),
    ("Outhouse", "non_operational_outhouse", "outhouse"),
    ("ManualGenerator", "non_operational_manual_generator", "manual generator"),
)
FUNCTIONAL_BUILDING_ALIAS_GROUPS = {
    "OxygenDiffuser": ("OxygenDiffuser", "MineralDeoxidizer"),
}
FUNCTIONAL_BUILDING_INCIDENT_IDS = tuple(item[1] for item in FUNCTIONAL_BUILDING_DIAGNOSTICS)

OPTIONAL_ORDINARY_RULE_INCIDENT_IDS = FUNCTIONAL_BUILDING_INCIDENT_IDS
ORDINARY_RULE_INCIDENT_IDS = (
    *BASE_ORDINARY_RULE_INCIDENT_IDS,
    *OPTIONAL_ORDINARY_RULE_INCIDENT_IDS,
)
BASE_RULE_INCIDENT_IDS = (*CANONICAL_ALERT_IDS, *BASE_ORDINARY_RULE_INCIDENT_IDS)
RULE_INCIDENT_IDS = (*CANONICAL_ALERT_IDS, *ORDINARY_RULE_INCIDENT_IDS)
_KNOWN_RULE_INCIDENT_IDS = frozenset(RULE_INCIDENT_IDS)


def rule_incident_catalog_blockers() -> List[str]:
    """Return structural blockers for the single rule-incident catalog.

    The verifier and runtime both consume the exported tuples above.  This
    check catches duplicate, malformed, overlapping, or incompletely composed
    entries before a new rule can silently disappear from validation.
    """

    blockers: List[str] = []
    groups = {
        "canonical": tuple(CANONICAL_ALERT_IDS),
        "base_ordinary": tuple(BASE_ORDINARY_RULE_INCIDENT_IDS),
        "optional_ordinary": tuple(OPTIONAL_ORDINARY_RULE_INCIDENT_IDS),
        "ordinary": tuple(ORDINARY_RULE_INCIDENT_IDS),
        "base": tuple(BASE_RULE_INCIDENT_IDS),
        "all": tuple(RULE_INCIDENT_IDS),
    }
    identifier_pattern = re.compile(r"[a-z][a-z0-9_]{1,63}")
    for name, values in groups.items():
        if any(not isinstance(item, str) or identifier_pattern.fullmatch(item) is None for item in values):
            blockers.append(f"{name}_id_invalid")
        if len(values) != len(set(values)):
            blockers.append(f"{name}_id_duplicate")
    if set(groups["canonical"]) & set(groups["ordinary"]):
        blockers.append("canonical_ordinary_overlap")
    if tuple(groups["ordinary"]) != tuple(groups["base_ordinary"] + groups["optional_ordinary"]):
        blockers.append("ordinary_catalog_composition_mismatch")
    if tuple(groups["base"]) != tuple(groups["canonical"] + groups["base_ordinary"]):
        blockers.append("base_catalog_composition_mismatch")
    if tuple(groups["all"]) != tuple(groups["canonical"] + groups["ordinary"]):
        blockers.append("rule_catalog_composition_mismatch")
    if tuple(groups["optional_ordinary"]) != tuple(FUNCTIONAL_BUILDING_INCIDENT_IDS):
        blockers.append("optional_catalog_functional_building_mismatch")
    return sorted(set(blockers))


def _signal(
    incident_id: str,
    state: str,
    title: str,
    risk: str,
    reason: str,
    action: str,
    evidence: Iterable[str],
    *,
    command: Optional[Dict[str, Any]] = None,
    required_consecutive_samples: int = 1,
    min_duration_seconds: float = 0.0,
    cooldown_seconds: float = 60.0,
) -> Dict[str, Any]:
    return {
        "incident_id": incident_id,
        "state": state,
        "title": title,
        "risk": risk,
        "reason": reason,
        "action": action,
        "evidence": list(evidence),
        "command": command,
        "required_consecutive_samples": required_consecutive_samples,
        "min_duration_seconds": min_duration_seconds,
        "cooldown_seconds": cooldown_seconds,
    }


def _unknown_signal(
    incident_id: str,
    title: str,
    risk: str,
    evidence: Sequence[str],
    *,
    command: Optional[Dict[str, Any]] = None,
    required_consecutive_samples: int = 1,
    min_duration_seconds: float = 0.0,
    cooldown_seconds: float = 60.0,
) -> Dict[str, Any]:
    return _signal(
        incident_id,
        "unknown",
        title,
        risk,
        "Required telemetry is unsupported, failed, or stale.",
        "Restore trustworthy telemetry before making a colony decision.",
        evidence,
        command=command,
        required_consecutive_samples=required_consecutive_samples,
        min_duration_seconds=min_duration_seconds,
        cooldown_seconds=cooldown_seconds,
    )


def survival_alert_policy_blockers() -> List[str]:
    blockers: List[str] = []
    if set(SURVIVAL_ALERT_POLICY) != set(CANONICAL_ALERT_IDS):
        blockers.append("canonical_alert_policy_not_closed")
    pause_ranks: List[int] = []
    for alert_id, policy in SURVIVAL_ALERT_POLICY.items():
        if not isinstance(alert_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", alert_id):
            blockers.append("canonical_alert_id_invalid")
        if set(policy) != _ALERT_POLICY_KEYS:
            blockers.append(f"alert_policy_shape_invalid:{alert_id}")
            continue
        disposition = policy.get("disposition")
        rank = policy.get("rank")
        if disposition == "pause":
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
                blockers.append(f"alert_policy_rank_invalid:{alert_id}")
            else:
                pause_ranks.append(rank)
        elif disposition == "observe":
            if rank is not None:
                blockers.append(f"alert_policy_observe_ranked:{alert_id}")
        else:
            blockers.append(f"alert_policy_disposition_invalid:{alert_id}")
        if policy.get("risk") not in {"high", "medium", "low"}:
            blockers.append(f"alert_policy_risk_invalid:{alert_id}")
        if any(not isinstance(policy.get(key), str) or not policy.get(key) for key in ("title", "action", "reason")):
            blockers.append(f"alert_policy_text_invalid:{alert_id}")
        samples = policy.get("samples")
        if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
            blockers.append(f"alert_policy_samples_invalid:{alert_id}")
        for key in ("duration", "cooldown"):
            value = policy.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                blockers.append(f"alert_policy_{key}_invalid:{alert_id}")
    if len(pause_ranks) != len(set(pause_ranks)):
        blockers.append("alert_policy_rank_duplicate")
    return sorted(set(blockers))


def survival_alert_policy_hash() -> str:
    return canonical_hash(SURVIVAL_ALERT_POLICY)


def _pause_command(alert_id: str) -> Dict[str, Any]:
    return {
        "action": "set_paused",
        "args": {"paused": True},
        "preconditions": [
            build_condition(
                "alerts", "contains", alert_id, ALERTS_CANONICAL_CONTRACT_ID
            ),
            build_condition(
                "game.paused", "eq", False, GAME_PAUSED_CONTRACT_ID
            ),
        ],
        "expected_effects": [
            build_condition(
                "game.paused", "eq", True, GAME_PAUSED_CONTRACT_ID
            )
        ],
    }


def _alert_signal(alert_id: str, active: bool) -> Dict[str, Any]:
    policy = SURVIVAL_ALERT_POLICY[alert_id]
    return _signal(
        alert_id,
        "active" if active else "inactive",
        str(policy["title"]),
        str(policy["risk"]),
        str(policy["reason"]),
        str(policy["action"]),
        ("alerts",),
        # Build a mutating pause candidate only for an active alert.  Inactive
        # signals are retained for incident-window accounting, but their
        # command would never be actionable and constructing its contract
        # declarations dominated large replay cost.
        command=(
            _pause_command(alert_id)
            if active and policy["disposition"] == "pause"
            else None
        ),
        required_consecutive_samples=int(policy["samples"]),
        min_duration_seconds=float(policy["duration"]),
        cooldown_seconds=float(policy["cooldown"]),
    )


def _evaluate_alert_signals(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    alerts_read = read_contract_field(state, "alerts", ALERTS_CANONICAL_CONTRACT_ID)
    if not alerts_read.usable or survival_alert_policy_blockers():
        return [
            _unknown_signal(
                alert_id,
                str(SURVIVAL_ALERT_POLICY[alert_id]["title"]),
                str(SURVIVAL_ALERT_POLICY[alert_id]["risk"]),
                ("alerts",),
                required_consecutive_samples=int(SURVIVAL_ALERT_POLICY[alert_id]["samples"]),
                min_duration_seconds=float(SURVIVAL_ALERT_POLICY[alert_id]["duration"]),
                cooldown_seconds=float(SURVIVAL_ALERT_POLICY[alert_id]["cooldown"]),
            )
            for alert_id in CANONICAL_ALERT_IDS
        ]
    active_alerts = set(alerts_read.value)
    return [_alert_signal(alert_id, alert_id in active_alerts) for alert_id in CANONICAL_ALERT_IDS]


def survival_incident_witness_blockers(
    state: Mapping[str, Any], incident: Mapping[str, Any]
) -> List[str]:
    """Validate the episode history required to grant pause authority."""

    blockers: List[str] = []
    alert_id = incident.get("incident_id")
    policy = SURVIVAL_ALERT_POLICY.get(alert_id) if isinstance(alert_id, str) else None
    if not isinstance(policy, Mapping) or policy.get("disposition") != "pause":
        return ["incident_witness_alert_invalid"]
    if incident.get("status") != "open":
        blockers.append("incident_witness_not_open")
    if incident.get("telemetry_state") != "active":
        blockers.append("incident_witness_telemetry_not_active")
    if incident.get("risk") != policy.get("risk"):
        blockers.append("incident_witness_risk_mismatch")
    if incident.get("session_id") != state.get("session_id"):
        blockers.append("incident_witness_session_mismatch")
    if incident.get("snapshot_id") != state.get("snapshot_id"):
        blockers.append("incident_witness_snapshot_mismatch")
    first_seq = incident.get("first_seq")
    last_seq = incident.get("last_seq")
    if (
        isinstance(first_seq, bool)
        or not isinstance(first_seq, int)
        or first_seq < 1
        or isinstance(last_seq, bool)
        or not isinstance(last_seq, int)
        or last_seq < 1
        or first_seq > last_seq
    ):
        blockers.append("incident_witness_sequence_range_invalid")
    if (
        isinstance(last_seq, bool)
        or not isinstance(last_seq, int)
        or last_seq < 1
        or last_seq != state.get("seq")
    ):
        blockers.append("incident_witness_sequence_mismatch")
    first_snapshot_seq = incident.get("first_snapshot_seq")
    last_snapshot_seq = incident.get("last_snapshot_seq")
    if (
        isinstance(first_snapshot_seq, bool)
        or not isinstance(first_snapshot_seq, int)
        or first_snapshot_seq < 1
        or isinstance(last_snapshot_seq, bool)
        or not isinstance(last_snapshot_seq, int)
        or last_snapshot_seq < 1
        or first_snapshot_seq > last_snapshot_seq
        or last_snapshot_seq != state.get("snapshot_seq")
        or (
            isinstance(last_snapshot_seq, int)
            and not isinstance(last_snapshot_seq, bool)
            and isinstance(state.get("seq"), int)
            and not isinstance(state.get("seq"), bool)
            and last_snapshot_seq > state.get("seq")
        )
    ):
        blockers.append("incident_witness_snapshot_sequence_mismatch")
    consecutive = incident.get("consecutive_samples")
    required_samples = policy.get("samples")
    if (
        isinstance(consecutive, bool)
        or not isinstance(consecutive, int)
        or isinstance(required_samples, bool)
        or not isinstance(required_samples, int)
        or consecutive < required_samples
    ):
        blockers.append("incident_witness_samples_insufficient")
    elif (
        isinstance(first_snapshot_seq, int)
        and not isinstance(first_snapshot_seq, bool)
        and isinstance(last_snapshot_seq, int)
        and not isinstance(last_snapshot_seq, bool)
        and last_snapshot_seq - first_snapshot_seq + 1 != consecutive
    ):
        blockers.append("incident_witness_sample_sequence_gap")

    first_seen = parse_snapshot_timestamp(incident.get("first_seen"))
    last_seen = parse_snapshot_timestamp(incident.get("last_seen"))
    state_ts = parse_snapshot_timestamp(state.get("ts"))
    if first_seen is None:
        blockers.append("incident_witness_first_seen_invalid")
    if last_seen is None:
        blockers.append("incident_witness_last_seen_invalid")
    if state_ts is None:
        blockers.append("incident_witness_state_ts_invalid")
    if first_seen is not None and last_seen is not None:
        if first_seen > last_seen:
            blockers.append("incident_witness_time_order_invalid")
        duration = policy.get("duration")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or (last_seen - first_seen).total_seconds() < float(duration)
        ):
            blockers.append("incident_witness_duration_insufficient")
    if last_seen is not None and state_ts is not None and last_seen != state_ts:
        blockers.append("incident_witness_not_current")
    return sorted(set(blockers))


def arbitrate_survival_emergency(
    state: Optional[Mapping[str, Any]],
    open_incidents: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Select at most one pause containment intent from trusted alert evidence."""

    result: Dict[str, Any] = {
        "status": "blocked",
        "blockers": [],
        "policy_hash": None,
        "game_paused": None,
        "active_pause_alert_ids": [],
        "eligible_incident_ids": [],
        "selected_alert_id": None,
        "selected_incident_id": None,
        "expected_signal": None,
        "command": None,
    }
    blockers = survival_alert_policy_blockers()
    if not blockers:
        result["policy_hash"] = survival_alert_policy_hash()
    if not isinstance(state, Mapping):
        blockers.append("state_unavailable")
    snapshot = dict(state) if isinstance(state, Mapping) else {}
    bridge = snapshot.get("bridge") if isinstance(snapshot.get("bridge"), Mapping) else {}
    if not (
        bridge.get("trust_ready") is True
        and bridge.get("stale") is False
        and bridge.get("replay_mode") is False
        and bridge.get("trust_blockers") == []
    ):
        blockers.append("state_not_trusted_live")
    alerts_read = read_contract_field(snapshot, "alerts", ALERTS_CANONICAL_CONTRACT_ID)
    paused_read = read_contract_field(snapshot, "game.paused", GAME_PAUSED_CONTRACT_ID)
    if not alerts_read.usable:
        blockers.append("alerts_contract_unusable")
    if not paused_read.usable or not isinstance(paused_read.value, bool):
        blockers.append("paused_contract_unusable")
    else:
        result["game_paused"] = paused_read.value
    if blockers:
        result["blockers"] = sorted(set(blockers))
        return result
    active_pause_ids = [
        alert_id
        for alert_id in alerts_read.value
        if SURVIVAL_ALERT_POLICY[alert_id]["disposition"] == "pause"
    ]
    result["active_pause_alert_ids"] = sorted(
        active_pause_ids,
        key=lambda item: int(SURVIVAL_ALERT_POLICY[item]["rank"]),
    )
    result["game_paused"] = paused_read.value
    reduced = _reduce_survival_arbitration(
        snapshot,
        result["active_pause_alert_ids"],
        paused_read.value,
        open_incidents,
    )
    result.update(reduced)
    result["policy_hash"] = survival_alert_policy_hash()
    return result


_SURVIVAL_CERTIFICATE_SCHEMA = "oni.survival-arbitration.v1"
_SURVIVAL_CERTIFICATE_KEYS = frozenset(
    {
        "schema",
        "policy_hash",
        "status",
        "blockers",
        "game_paused",
        "active_pause_alert_ids",
        "eligible_incident_ids",
        "selected_incident_id",
        "command_hint_hash",
    }
)

# These blockers are emitted before the original state can be reduced to a
# trustworthy active-alert/witness view. A frame may carry such a blocked
# projection, but it can never carry an authorizing selected command.
_SURVIVAL_UNRECONSTRUCTABLE_BLOCKERS = frozenset(
    {
        "state_unavailable",
        "state_not_trusted_live",
        "alerts_contract_unusable",
        "paused_contract_unusable",
    }
)


def _empty_survival_arbitration_result() -> Dict[str, Any]:
    return {
        "status": "blocked",
        "blockers": [],
        "game_paused": None,
        "active_pause_alert_ids": [],
        "eligible_incident_ids": [],
        "selected_alert_id": None,
        "selected_incident_id": None,
        "expected_signal": None,
        "command": None,
    }


def _ranked_pause_alert_ids() -> List[str]:
    values = [
        alert_id
        for alert_id, policy in SURVIVAL_ALERT_POLICY.items()
        if isinstance(policy, Mapping) and policy.get("disposition") == "pause"
    ]
    try:
        return sorted(values, key=lambda item: int(SURVIVAL_ALERT_POLICY[item]["rank"]))
    except (KeyError, TypeError, ValueError):
        # Policy publication remains fail closed; diagnostics still need a
        # deterministic order while describing a malformed policy.
        return sorted(values)


def _normalize_survival_active_ids(value: Any) -> Tuple[List[str], List[str]]:
    blockers: List[str] = []
    pause_ids = set(_ranked_pause_alert_ids())
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return [], ["survival_active_alerts_invalid"]
    raw = list(value)
    if any(not isinstance(item, str) or item not in pause_ids for item in raw):
        blockers.append("survival_active_alerts_invalid")
    known = [item for item in raw if isinstance(item, str) and item in pause_ids]
    if len(known) != len(set(known)):
        blockers.append("survival_active_alerts_duplicate")
    try:
        ordered = sorted(known, key=lambda item: int(SURVIVAL_ALERT_POLICY[item]["rank"]))
    except (KeyError, TypeError, ValueError):
        ordered = sorted(known)
    if known != ordered:
        blockers.append("survival_active_alerts_unordered")
    return ordered, sorted(set(blockers))


def _normalize_survival_incidents(value: Any) -> Tuple[List[Mapping[str, Any]], List[str]]:
    blockers: List[str] = []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return [], ["open_incidents_invalid"]
    raw = list(value)
    if any(not isinstance(item, Mapping) for item in raw):
        return [], ["open_incidents_invalid"]
    records = [item for item in raw if isinstance(item, Mapping)]
    incident_ids = [item.get("incident_id") for item in records]
    valid_ids = [item for item in incident_ids if isinstance(item, str) and item]
    if len(valid_ids) != len(incident_ids):
        blockers.append("open_incidents_invalid")
    if len(valid_ids) != len(set(valid_ids)):
        blockers.append("open_incidents_duplicate")
    if any(item not in _KNOWN_RULE_INCIDENT_IDS for item in valid_ids):
        blockers.append("open_incident_alert_invalid")
    return records, sorted(set(blockers))


def _strict_survival_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_strict_survival_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(_strict_survival_equal(left[key], right[key]) for key in left)
        )
    return type(left) is type(right) and left == right


def _reduce_survival_arbitration(
    state_context: Mapping[str, Any],
    active_pause_alert_ids: Any,
    game_paused: Any,
    incident_items: Any,
) -> Dict[str, Any]:
    """Reduce one bounded alert/witness view into the canonical result.

    This function intentionally does not authenticate the source state. The
    caller supplies that context after its own state/trust/contract checks;
    certificate validation supplies only the frame-bound context it can
    reproduce. Both paths therefore share active-set, witness, hysteresis,
    priority, and containment semantics without treating a frame digest as
    provenance.
    """

    result = _empty_survival_arbitration_result()
    active_ids, active_blockers = _normalize_survival_active_ids(active_pause_alert_ids)
    records, incident_blockers = _normalize_survival_incidents(incident_items)
    result["active_pause_alert_ids"] = list(active_ids)
    if isinstance(game_paused, bool):
        result["game_paused"] = game_paused
    blockers = list(active_blockers) + list(incident_blockers)
    if not isinstance(state_context, Mapping):
        blockers.append("survival_state_context_invalid")
        context: Mapping[str, Any] = {}
    else:
        context = state_context
    if not isinstance(game_paused, bool):
        blockers.append("paused_contract_unusable")

    active_set = set(active_ids)
    # pending/cooldown are the tracker's hysteresis states for an alert that is
    # active but not yet authorizing.  Treat them like an absent witness
    # (pending_hysteresis) so frame certificates, advice and candidate
    # admission agree whether or not the caller filtered the list to open
    # records (code review 2026-09-26, L9).
    hysteresis_statuses = {"pending", "cooldown"}
    relevant_records = [
        item
        for item in records
        if item.get("status") == "open"
        or (
            item.get("incident_id") in active_set
            and item.get("status") not in hysteresis_statuses
        )
    ]
    # The incidents payload contains retained inactive/cooldown history. It is
    # ignored unless its id is active now; any other non-open active witness
    # (resolved, inactive, blocked_unknown) contradicts the active alert and is
    # an invalid authorizing episode rather than a reason to silently omit it.
    if any(
        item.get("incident_id") in active_set and item.get("status") != "open"
        for item in relevant_records
    ):
        blockers.append("open_incident_status_invalid")
    if blockers:
        result["blockers"] = sorted(set(blockers))
        return result

    if game_paused is True:
        result.update({"status": "none", "blockers": ["already_contained"]})
        return result

    pause_ids = set(_ranked_pause_alert_ids())
    witness_by_id = {
        str(item["incident_id"]): item
        for item in relevant_records
        if isinstance(item.get("incident_id"), str)
        and item.get("incident_id") in pause_ids
    }
    for alert_id in sorted(
        set(witness_by_id) - active_set,
        key=lambda item: int(SURVIVAL_ALERT_POLICY[item]["rank"]),
    ):
        if witness_by_id[alert_id].get("status") == "open":
            blockers.append(f"incident_witness_alert_inactive:{alert_id}")

    eligible: List[str] = []
    pending_hysteresis = False
    for alert_id in active_ids:
        witness = witness_by_id.get(alert_id)
        policy = SURVIVAL_ALERT_POLICY[alert_id]
        if witness is None:
            if int(policy["samples"]) == 1 and float(policy["duration"]) == 0.0:
                blockers.append(f"incident_witness_missing:{alert_id}")
            else:
                pending_hysteresis = True
            continue
        witness_blockers = survival_incident_witness_blockers(context, witness)
        blockers.extend(f"{item}:{alert_id}" for item in witness_blockers)
        if not witness_blockers:
            eligible.append(alert_id)

    eligible.sort(key=lambda item: int(SURVIVAL_ALERT_POLICY[item]["rank"]))
    if eligible:
        selected_rank = int(SURVIVAL_ALERT_POLICY[eligible[0]]["rank"])
        for alert_id in active_ids:
            policy = SURVIVAL_ALERT_POLICY[alert_id]
            if (
                alert_id not in witness_by_id
                and int(policy["rank"]) < selected_rank
                and int(policy["samples"]) > 1
            ):
                blockers.append(f"incident_witness_higher_priority_missing:{alert_id}")
    result["eligible_incident_ids"] = list(eligible)
    if blockers:
        result["blockers"] = sorted(set(blockers))
        return result
    if not eligible:
        result.update(
            {
                "status": "none",
                "blockers": ["pending_hysteresis"] if pending_hysteresis else [],
            }
        )
        return result
    selected = eligible[0]
    signal = _alert_signal(selected, True)
    result.update(
        {
            "status": "selected",
            "blockers": [],
            "selected_alert_id": selected,
            "selected_incident_id": selected,
            "expected_signal": copy.deepcopy(signal),
            "command": copy.deepcopy(signal["command"]),
        }
    )
    return result


def build_survival_arbitration_certificate(
    state: Optional[Mapping[str, Any]], incidents: Iterable[Mapping[str, Any]]
) -> Dict[str, Any]:
    try:
        incident_list = list(incidents)
    except TypeError:
        incident_list = incidents
    arbitration = arbitrate_survival_emergency(state, incident_list)  # type: ignore[arg-type]
    command = arbitration.get("command")
    return {
        "schema": _SURVIVAL_CERTIFICATE_SCHEMA,
        "policy_hash": arbitration.get("policy_hash"),
        "status": arbitration.get("status"),
        "blockers": list(arbitration.get("blockers") or []),
        "game_paused": arbitration.get("game_paused"),
        "active_pause_alert_ids": list(arbitration.get("active_pause_alert_ids") or []),
        "eligible_incident_ids": list(arbitration.get("eligible_incident_ids") or []),
        "selected_incident_id": arbitration.get("selected_incident_id"),
        "command_hint_hash": (
            canonical_hash(command) if isinstance(command, Mapping) else None
        ),
    }


def survival_certificate_blockers(
    certificate: Any,
    frame_source: Mapping[str, Any],
    incident_items: Sequence[Mapping[str, Any]],
) -> List[str]:
    """Validate the bounded frame certificate used at queue and approval time.

    The certificate is a claim about a frame-bound view, not a second
    arbitration implementation. After shape checks this function runs the
    same pure reducer as :func:`arbitrate_survival_emergency` and compares
    every result field, including blockers and priority order.
    """

    blockers: List[str] = []
    if not isinstance(certificate, Mapping) or set(certificate) != _SURVIVAL_CERTIFICATE_KEYS:
        return ["survival_certificate_shape_invalid"]
    if certificate.get("schema") != _SURVIVAL_CERTIFICATE_SCHEMA:
        blockers.append("survival_certificate_schema_invalid")
    try:
        expected_policy_hash = survival_alert_policy_hash()
    except Exception:  # noqa: BLE001 - policy validation below remains fail closed
        expected_policy_hash = None
    if certificate.get("policy_hash") != expected_policy_hash:
        blockers.append("survival_certificate_policy_mismatch")
    status = certificate.get("status")
    if status not in {"selected", "none", "blocked"}:
        blockers.append("survival_certificate_status_invalid")
    raw_blockers = certificate.get("blockers")
    raw_blockers_valid = (
        isinstance(raw_blockers, list)
        and all(isinstance(item, str) and bool(item) and len(item) <= 200 for item in raw_blockers)
        and raw_blockers == sorted(set(raw_blockers))
    )
    if not raw_blockers_valid:
        blockers.append("survival_certificate_blockers_invalid")
        raw_blockers = []
    game_paused = certificate.get("game_paused")
    if game_paused is not None and not isinstance(game_paused, bool):
        blockers.append("survival_certificate_paused_invalid")
    semantic_game_paused = game_paused if isinstance(game_paused, bool) else None

    pause_ids = set(_ranked_pause_alert_ids())

    def checked_ids(key: str) -> List[str]:
        raw = certificate.get(key)
        if (
            not isinstance(raw, list)
            or any(not isinstance(item, str) or item not in pause_ids for item in raw)
            or len(raw) != len(set(raw))
        ):
            blockers.append(f"survival_certificate_{key}_invalid")
            return []
        try:
            expected = sorted(raw, key=lambda item: int(SURVIVAL_ALERT_POLICY[item]["rank"]))
        except (KeyError, TypeError, ValueError):
            blockers.append(f"survival_certificate_{key}_invalid")
            return []
        if raw != expected:
            blockers.append(f"survival_certificate_{key}_unordered")
        return list(raw)

    active_ids = checked_ids("active_pause_alert_ids")
    eligible_ids = checked_ids("eligible_incident_ids")
    if not set(eligible_ids).issubset(active_ids):
        blockers.append("survival_certificate_eligible_not_active")

    if not isinstance(frame_source, Mapping):
        blockers.append("survival_certificate_source_invalid")
        semantic_source: Dict[str, Any] = {}
    else:
        semantic_source = dict(frame_source)

    # Older certificate archives may omit snapshot_seq from the frame source.
    # Preserve audit compatibility by deriving it only when every represented
    # active witness agrees; current decision-frame.v2 producers bind it
    # explicitly and therefore take precedence. A disagreement remains fail
    # closed through the shared witness reducer.
    if "snapshot_seq" not in semantic_source:
        # Legacy archives may omit this binding. Infer it only when all active
        # witnesses agree; an explicitly present invalid value must remain
        # invalid so the reducer fails closed instead of silently repairing it.
        snapshot_sequences = {
            item.get("last_snapshot_seq")
            for item in incident_items
            if isinstance(item, Mapping)
            and isinstance(item.get("last_snapshot_seq"), int)
            and not isinstance(item.get("last_snapshot_seq"), bool)
            and item.get("incident_id") in set(active_ids)
        }
        if len(snapshot_sequences) == 1:
            semantic_source["snapshot_seq"] = next(iter(snapshot_sequences))
    expected = _reduce_survival_arbitration(
        semantic_source,
        active_ids,
        semantic_game_paused,
        incident_items,
    )

    # A state/trust/contract blocker can stop the producer before the active
    # set is available. Such a projection is allowed only as a safe blocked
    # envelope with no eligible/selected command payload. All other blocked
    # results must be exactly reproducible by the reducer below.
    unreconstructable = (
        set(raw_blockers).intersection(_SURVIVAL_UNRECONSTRUCTABLE_BLOCKERS)
        if raw_blockers_valid
        else set()
    )
    if unreconstructable:
        if status != "blocked":
            blockers.append("survival_certificate_context_block_not_blocked")
        if (
            active_ids
            or eligible_ids
            or certificate.get("selected_incident_id") is not None
            or certificate.get("command_hint_hash") is not None
        ):
            blockers.append("survival_certificate_context_block_payload")
        if not set(raw_blockers).issubset(_SURVIVAL_UNRECONSTRUCTABLE_BLOCKERS):
            blockers.append("survival_certificate_context_block_reason_invalid")
        return sorted(set(blockers))

    def compare(field: str, actual: Any, expected_value: Any, label: str) -> None:
        if not _strict_survival_equal(actual, expected_value):
            blockers.append(f"survival_certificate_{label}_mismatch")

    if status in {"selected", "none", "blocked"}:
        compare("status", status, expected.get("status"), "status")
        compare("game_paused", game_paused, expected.get("game_paused"), "paused")
        compare(
            "active_pause_alert_ids",
            active_ids,
            expected.get("active_pause_alert_ids"),
            "active_alerts",
        )
        compare(
            "eligible_incident_ids",
            eligible_ids,
            expected.get("eligible_incident_ids"),
            "eligibility",
        )
        compare(
            "selected_incident_id",
            certificate.get("selected_incident_id"),
            expected.get("selected_incident_id"),
            "selection",
        )
        if raw_blockers_valid:
            compare("blockers", raw_blockers, expected.get("blockers"), "blockers")
        expected_command = expected.get("command")
        expected_command_hash = (
            canonical_hash(expected_command) if isinstance(expected_command, Mapping) else None
        )
        compare(
            "command_hint_hash",
            certificate.get("command_hint_hash"),
            expected_command_hash,
            "command_hash",
        )

    selected = certificate.get("selected_incident_id")
    command_hint_hash = certificate.get("command_hint_hash")
    if status == "selected":
        if raw_blockers:
            blockers.append("survival_certificate_selected_with_blockers")
        if game_paused is not False:
            blockers.append("survival_certificate_selected_while_paused")
        if selected not in pause_ids:
            blockers.append("survival_certificate_selected_id_invalid")
    else:
        if selected is not None or command_hint_hash is not None:
            blockers.append("survival_certificate_nonselected_payload")
        if status == "blocked" and not raw_blockers:
            blockers.append("survival_certificate_blocked_without_reason")
        if status == "none" and game_paused is True and raw_blockers != ["already_contained"]:
            blockers.append("survival_certificate_containment_mismatch")
    return sorted(set(blockers))


def survival_suggested_command_frame_blockers(
    command: Mapping[str, Any], frame: Mapping[str, Any]
) -> List[str]:
    """Bind one suggested pause envelope to its certified alert episode."""

    if command.get("mode") != "suggested" or command.get("action") != "set_paused":
        return []
    blockers: List[str] = []
    projections = frame.get("projections") if isinstance(frame.get("projections"), Mapping) else {}
    incidents_projection = (
        projections.get("incidents")
        if isinstance(projections.get("incidents"), Mapping)
        else {}
    )
    advice_projection = (
        projections.get("advice")
        if isinstance(projections.get("advice"), Mapping)
        else {}
    )
    incident_items = (
        incidents_projection.get("items")
        if isinstance(incidents_projection.get("items"), list)
        else []
    )
    certificate = incidents_projection.get("survival_emergency")
    frame_source = frame.get("source") if isinstance(frame.get("source"), Mapping) else {}
    blockers.extend(
        survival_certificate_blockers(certificate, frame_source, incident_items)
    )
    if (
        incidents_projection.get("valid") is not True
        or incidents_projection.get("truncated") is not False
        or incidents_projection.get("count") != len(incident_items)
    ):
        blockers.append("survival_incident_projection_incomplete")
    advice_items = (
        advice_projection.get("items")
        if isinstance(advice_projection.get("items"), list)
        else []
    )
    if (
        advice_projection.get("valid") is not True
        or advice_projection.get("truncated") is not False
        or advice_projection.get("count") != len(advice_items)
    ):
        blockers.append("survival_advice_projection_incomplete")

    selected = certificate.get("selected_incident_id") if isinstance(certificate, Mapping) else None
    if selected != command.get("source_incident_id"):
        blockers.append("survival_command_selection_mismatch")
        return sorted(set(blockers))
    expected_signal = _alert_signal(str(selected), True) if selected in SURVIVAL_ALERT_POLICY else {}
    command_hint = {
        "action": command.get("action"),
        "args": command.get("args"),
        "preconditions": command.get("preconditions"),
        "expected_effects": command.get("expected_effects"),
    }
    try:
        if canonical_hash(command_hint) != canonical_hash(expected_signal.get("command")):
            blockers.append("survival_command_hint_mismatch")
    except Exception:  # noqa: BLE001 - malformed command remains non-authorizing
        blockers.append("survival_command_hint_invalid")
    source = command.get("source") if isinstance(command.get("source"), Mapping) else {}
    selected_witnesses = [
        item for item in incident_items
        if isinstance(item, Mapping) and item.get("incident_id") == selected
    ]
    matching_advice = [
        item for item in advice_items
        if isinstance(item, Mapping)
        and item.get("incident_id") == selected
        and item.get("risk") == command.get("risk")
        and item.get("incident_opened_at") == source.get("incident_opened_at")
    ]
    if len(selected_witnesses) != 1:
        blockers.append("survival_selected_witness_missing")
    elif selected_witnesses[0].get("first_seen") != source.get("incident_opened_at"):
        blockers.append("survival_incident_opened_at_mismatch")
    if len(matching_advice) != 1:
        blockers.append("survival_advice_binding_mismatch")
    if (
        source.get("kind") != "rule_incident"
        or source.get("title") != expected_signal.get("title")
        or command.get("risk") != expected_signal.get("risk")
        or command.get("reason") != expected_signal.get("reason")
        or command.get("evidence") != expected_signal.get("evidence")
    ):
        blockers.append("survival_rule_metadata_mismatch")
    return sorted(set(blockers))


def evaluate_signals(state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(state, dict):
        return []

    signals: List[Dict[str, Any]] = []

    food_paths = ("resources.food_kcal", "duplicants.total")
    food_read = read_contract_field(
        state,
        "resources.food_kcal",
        REGISTERED_FOOD_CONTRACT_ID,
    )
    dupes_read = read_contract_field(
        state,
        "duplicants.total",
        DUPLICANTS_TOTAL_CONTRACT_ID,
    )
    if not food_read.usable or not dupes_read.usable or dupes_read.value <= 0:
        signals.append(
            _unknown_signal(
                "low_food",
                "Registered food telemetry is unavailable",
                "high",
                food_paths,
            )
        )
    else:
        food_kcal = float(food_read.value)
        dupes = int(dupes_read.value)
        risk_threshold_kcal = dupes * 2000.0
        if food_read.status == "estimated":
            signals.append(
                _signal(
                    "low_food",
                    "unknown",
                    "Enumerated food lower bound leaves the total unknown",
                    "high",
                    f"The enumerated registered-food lower bound is {food_kcal:.1f} kcal versus the "
                    f"{risk_threshold_kcal:.1f} kcal diagnostic threshold for {dupes} active-world "
                    "duplicants. A bounded lower bound cannot establish that total registered or "
                    "accessible food is low and is not a food-runway estimate.",
                    "Wait for a complete registered inventory sample and inspect accessibility before deciding.",
                    food_paths,
                    command=None,
                    cooldown_seconds=60,
                )
            )
            return signals + _evaluate_non_food_signals(state)

        active = food_kcal < risk_threshold_kcal
        title = "Registered food is below the risk threshold"
        reason = (
            f"The complete registered non-private food sum is {food_kcal:.1f} kcal versus the "
            f"{risk_threshold_kcal:.1f} kcal risk threshold for {dupes} active-world duplicants. "
            "This is a conservative inventory diagnostic, not a food-runway estimate."
        )
        message = (
            f"Complete registered food sum {food_kcal:.1f} kcal is below the "
            f"{risk_threshold_kcal:.1f} kcal risk threshold."
        )
        signals.append(
            _signal(
                "low_food",
                "active" if active else "inactive",
                title,
                "high",
                reason,
                "Inspect food accessibility and production before expanding.",
                food_paths,
                command=(
                    {
                        "action": "focus_note",
                        "args": {
                            "tag": "food_risk",
                            "message": message,
                        },
                        "preconditions": [
                            build_condition(
                                "resources.food_kcal",
                                "lt",
                                risk_threshold_kcal,
                                REGISTERED_FOOD_CONTRACT_ID,
                                quality=("observed",),
                            ),
                            build_condition(
                                "duplicants.total",
                                "eq",
                                dupes,
                                DUPLICANTS_TOTAL_CONTRACT_ID,
                            ),
                        ],
                        "expected_effects": [],
                    }
                    if active
                    else None
                ),
                cooldown_seconds=60,
            )
        )

    signals.extend(_evaluate_non_food_signals(state))
    return signals


def _evaluate_non_food_signals(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    signals.extend(_evaluate_alert_signals(state))

    idle_read = read_contract_field(state, "duplicants.idle", DUPLICANTS_IDLE_CONTRACT_ID)
    if not idle_read.usable:
        signals.append(
            _unknown_signal(
                "idle_duplicants",
                "Duplicants are idle",
                "medium",
                ("duplicants.idle",),
                required_consecutive_samples=2,
                min_duration_seconds=5,
            )
        )
    else:
        idle = int(idle_read.value)
        signals.append(
            _signal(
                "idle_duplicants",
                "active" if idle > 0 else "inactive",
                "Duplicants are idle",
                "medium",
                f"{idle} duplicant(s) are idle.",
                "Check errands, priorities, and reachable work before adding systems.",
                ("duplicants.idle",),
                command=(
                    {
                        "action": "focus_note",
                        "args": {"tag": "idle_dupes", "message": f"{idle} duplicant(s) are idle."},
                        "preconditions": [
                            build_condition(
                                "duplicants.idle",
                                "gt",
                                0,
                                DUPLICANTS_IDLE_CONTRACT_ID,
                            )
                        ],
                        "expected_effects": [],
                    }
                    if idle > 0
                    else None
                ),
                required_consecutive_samples=2,
                min_duration_seconds=5,
                cooldown_seconds=120,
            )
        )

    stressed_read = read_contract_field(
        state,
        "duplicants.stressed",
        DUPLICANTS_STRESSED_CONTRACT_ID,
    )
    if not stressed_read.usable:
        signals.append(
            _unknown_signal(
                "stressed_duplicants",
                "Stress needs attention",
                "medium",
                ("duplicants.stressed",),
                required_consecutive_samples=2,
                min_duration_seconds=5,
            )
        )
    else:
        stressed = int(stressed_read.value)
        signals.append(
            _signal(
                "stressed_duplicants",
                "active" if stressed > 0 else "inactive",
                "Stress needs attention",
                "medium",
                f"{stressed} duplicant(s) are stressed.",
                "Review downtime, decor, morale, and high-stress errands.",
                ("duplicants.stressed",),
                required_consecutive_samples=2,
                min_duration_seconds=5,
                cooldown_seconds=120,
            )
        )

    research_read = read_contract_field(
        state,
        "research.current",
        RESEARCH_CURRENT_CONTRACT_ID,
    )
    if not research_read.usable:
        signals.append(
            _unknown_signal(
                "no_active_research",
                "No active research detected",
                "low",
                ("research.current",),
            )
        )
    else:
        current_research = research_read.value
        active = current_research == ""
        signals.append(
            _signal(
                "no_active_research",
                "active" if active else "inactive",
                "No active research detected",
                "low",
                "The observed current research id is empty.",
                "If a research station is available, choose the next survival-critical technology.",
                ("research.current",),
                # The empty selection is trustworthy, but the next technology is
                # not.  Keep this as an observation/advice incident until a
                # current, feasibility-checked research catalog selects an exact
                # id.  Explicit operator/plan ``set_research`` commands remain
                # available through the command pipeline.
                command=None,
                cooldown_seconds=300,
            )
        )

    for key, label in (
        ("oxygen_diffuser", "oxygen diffuser"),
        ("outhouse", "outhouse"),
        ("manual_generator", "manual generator"),
    ):
        path = f"buildings.{key}"
        incident_id = "missing_" + key
        count_read = read_contract_field(
            state,
            path,
            BUILDING_COMPLETED_COUNT_CONTRACT_ID,
        )
        if not count_read.usable:
            signals.append(_unknown_signal(incident_id, f"Missing {label}", "medium", (path,)))
            continue
        count = int(count_read.value)
        signals.append(
            _signal(
                incident_id,
                "active" if count == 0 else "inactive",
                f"Missing {label}",
                "medium",
                f"The complete active-world completed-prefab count for {key} is {count}.",
                f"Confirm the colony has a functional {label} or an equivalent replacement; the count alone does not prove function.",
                (path,),
                command=(
                    {
                        "action": "focus_note",
                        "args": {
                            "tag": incident_id,
                            "message": f"No {label} was observed in the latest trustworthy snapshot.",
                        },
                        "preconditions": [
                            build_condition(
                                path,
                                "eq",
                                0,
                                BUILDING_COMPLETED_COUNT_CONTRACT_ID,
                            )
                        ],
                        "expected_effects": [],
                    }
                    if count == 0
                    else None
                ),
                cooldown_seconds=300,
            )
        )

    signals.extend(_evaluate_functional_building_signals(state))
    return signals


def _functional_buildings_field_present(state: Mapping[str, Any]) -> bool:
    if "functional_buildings" in state:
        return True
    quality = state.get("quality")
    return isinstance(quality, Mapping) and "functional_buildings" in quality


def _read_functional_building_instances(
    state: Mapping[str, Any],
) -> Optional[Dict[str, List[Mapping[str, Any]]]]:
    """Return only a complete, contract-valid active-world instance view."""

    read = read_contract_field(
        state,
        "functional_buildings",
        FUNCTIONAL_BUILDINGS_CONTRACT_ID,
    )
    if not read.usable or not isinstance(read.value, Mapping):
        return None

    payload = read.value
    if payload.get("complete") is not True or payload.get("world_id") != state.get("world_id"):
        return None
    instances = payload.get("instances")
    if not isinstance(instances, list):
        return None

    grouped: Dict[str, List[Mapping[str, Any]]] = {
        prefab_id: [] for prefab_id in FUNCTIONAL_BUILDING_PREFAB_IDS
    }
    for instance in instances:
        if not isinstance(instance, Mapping):
            return None
        prefab_id = instance.get("prefab_id")
        if prefab_id not in grouped:
            return None
        # The field contract performs the complete identity/type/world check;
        # retain these explicit checks in the production consumer so a future
        # producer cannot turn malformed values into a known diagnosis.
        if (
            isinstance(instance.get("instance_id"), bool)
            or not isinstance(instance.get("instance_id"), int)
            or instance.get("instance_id") <= 0
            or instance.get("world_id") != state.get("world_id")
            or (instance.get("operational") is not None
                and not isinstance(instance.get("operational"), bool))
        ):
            return None
        grouped[prefab_id].append(instance)
    return grouped


def _evaluate_functional_building_signals(
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    # Older snapshots do not contain this additive field.  They keep their
    # existing rule catalog; a present-but-invalid field is different and must
    # fail closed with an unknown incident for each supported prefab.
    if not _functional_buildings_field_present(state):
        return []

    grouped = _read_functional_building_instances(state)
    signals: List[Dict[str, Any]] = []
    for prefab_id, incident_id, label in FUNCTIONAL_BUILDING_DIAGNOSTICS:
        evidence = ("functional_buildings",)
        if grouped is None:
            signals.append(
                _unknown_signal(
                    incident_id,
                    f"Native {label} operational state unavailable",
                    "medium",
                    evidence,
                    command=None,
                )
            )
            continue

        instances = [
            instance
            for native_id in FUNCTIONAL_BUILDING_ALIAS_GROUPS.get(prefab_id, (prefab_id,))
            for instance in grouped.get(native_id, [])
        ]
        if not instances:
            signals.append(
                _signal(
                    incident_id,
                    "inactive",
                    f"Native {label} operational check",
                    "medium",
                    f"No completed {label} instance was present in the complete active-world view.",
                    f"Use the existing missing-{label} rule to assess whether a {label} is needed.",
                    evidence,
                    command=None,
                )
            )
            continue

        if any(instance.get("operational") is None for instance in instances):
            signals.append(
                _unknown_signal(
                    incident_id,
                    f"Native {label} operational state unavailable",
                    "medium",
                    evidence,
                    command=None,
                )
            )
            continue

        non_operational = [
            instance for instance in instances if instance.get("operational") is False
        ]
        if not non_operational:
            signals.append(
                _signal(
                    incident_id,
                    "inactive",
                    f"Native {label} operational check",
                    "medium",
                    f"Every observed completed {label} instance reported native Operational.IsOperational=true.",
                    f"Continue observing the {label} without treating this flag as proof of throughput or sustainability.",
                    evidence,
                    command=None,
                )
            )
            continue

        instance_ids = ", ".join(
            str(instance["instance_id"])
            for instance in non_operational
        )
        signals.append(
            _signal(
                incident_id,
                "active",
                f"Native {label} operational check failed",
                "medium",
                f"Native Operational.IsOperational=false was observed for {label} instance id(s) {instance_ids} in the active world. The flag does not identify the specific unmet requirement.",
                f"Inspect the native operational requirements for {label} instance id(s) {instance_ids}; do not infer a fuel, power, access, pressure, or blockage cause from this flag alone.",
                evidence,
                command=None,
            )
        )

    return signals


def advice_from_incidents(incidents: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    advice: List[Dict[str, Any]] = []
    for incident in incidents:
        if incident.get("status") != "open":
            continue
        item = {
            "incident_id": incident.get("incident_id"),
            "title": incident.get("title"),
            "risk": incident.get("risk"),
            "reason": incident.get("reason"),
            "action": incident.get("action"),
            "evidence": incident.get("evidence"),
            "source_snapshot_id": incident.get("snapshot_id"),
            "incident_opened_at": incident.get("first_seen"),
        }
        command = incident.get("command")
        if isinstance(command, dict):
            item["command"] = command
        advice.append(item)
    return advice


def build_arbitrated_advice(
    state: Optional[Mapping[str, Any]], incidents: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Build advice while granting at most one emergency pause suggestion."""

    incident_list = [copy.deepcopy(item) for item in incidents]
    advice = advice_from_incidents(incident_list)
    for item in advice:
        command = item.get("command")
        if isinstance(command, Mapping) and command.get("action") == "set_paused":
            item.pop("command", None)
    arbitration = arbitrate_survival_emergency(
        state,
        [item for item in incident_list if item.get("status") == "open"],
    )
    if arbitration["status"] != "selected":
        return advice
    selected_id = arbitration["selected_incident_id"]
    matching = [item for item in advice if item.get("incident_id") == selected_id]
    expected = arbitration["expected_signal"]
    if len(matching) != 1 or not isinstance(expected, Mapping):
        return advice
    selected_item = matching[0]
    if not all(
        selected_item.get(key) == expected.get(key)
        for key in ("title", "risk", "reason", "action", "evidence")
    ):
        return advice
    selected_item["command"] = copy.deepcopy(arbitration["command"])
    return [selected_item] + [item for item in advice if item is not selected_item]


def evaluate_rules(state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    tracker = IncidentTracker()
    tracker.update(state, evaluate_signals(state))
    return build_arbitrated_advice(state, tracker.open_incidents())


def build_advice_markdown(advice: List[Dict[str, Any]], updated_at: str) -> str:
    lines = ["# ONI AI Advice", "", f"Updated: {updated_at}", ""]
    if not advice:
        lines.append("No open rule incidents from trustworthy telemetry.")
        return "\n".join(lines) + "\n"

    for index, item in enumerate(advice, start=1):
        lines.extend(
            [
                f"## {index}. {item.get('title')}",
                "",
                f"- Incident: {item.get('incident_id')}",
                f"- Risk: {item.get('risk')}",
                f"- Reason: {item.get('reason')}",
                f"- Suggested action: {item.get('action')}",
                f"- Evidence: {', '.join(item.get('evidence') or [])}",
            ]
        )
        command = item.get("command")
        if isinstance(command, dict):
            lines.append(f"- Pending command candidate: {command.get('action')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
