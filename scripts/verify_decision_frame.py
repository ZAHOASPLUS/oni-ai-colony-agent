#!/usr/bin/env python3
"""Offline contract verification for ``oni.decision-frame.v2``."""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.decision import (  # noqa: E402
    SUSTAINABILITY_GATE_IDS,
    DecisionFrameError,
    build_decision_frame,
    validate_decision_frame,
)
from agent.state_quality import canonical_state_hash  # noqa: E402


SAVE_HASH = "sha256:" + "a" * 64


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def blocker_codes(frame: Dict[str, Any]) -> set[str]:
    return {str(item["code"]) for item in frame["blockers"]}


def objective(objective_id: str, *, phase: str = "expand", status: str = "satisfied") -> Dict[str, Any]:
    return {
        "id": objective_id,
        "title": "intentionally omitted from the frame",
        "phase": phase,
        "priority": 50,
        "status": status,
        "reason": "fixture",
        "evidence": [],
        "completion_criteria": [],
        "required_telemetry": [],
        "recommended_actions": [],
    }


def command_pipeline(
    *,
    proposal_blockers: Iterable[str] = (),
    confirmation_blockers: Iterable[str] = (),
    candidate_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    proposal = sorted(set(proposal_blockers))
    confirmation = sorted(set(confirmation_blockers))
    candidates = sorted(set(candidate_ids))
    return {
        "schema": "oni.command-pipeline-gate.v1",
        "ready": not proposal,
        "blockers": proposal,
        "readiness": {
            "mutation_proposal": {"ready": not proposal, "blockers": proposal},
            "mutation_confirmation": {
                "ready": not confirmation,
                "blockers": confirmation,
                "candidate_ids": candidates,
            },
        },
        "counts": {
            "pending": 0,
            "approving": 0,
            "approved": 0,
            "executing": 0,
            "confirming": len(candidates),
            "failed": 0,
        },
        "in_flight_mutation_count": len(candidates),
        "ambiguous_outcome_count": 0,
        "duplicate_id_count": 0,
        "stale_pending_count": 0,
        "invalid_record_count": 0,
        "proposal_frame_evaluated": False,
        "proposal_frame_binding": "deferred_until_sync_pending_commands",
    }


def healthy_inputs(
    *,
    actions_enabled: bool = False,
    suggestions_enabled: bool = True,
    include_command_pipeline: bool = True,
) -> Dict[str, Dict[str, Any]]:
    state: Dict[str, Any] = {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "session_id": "session-decision-frame",
        "save_id_hash": SAVE_HASH,
        "save_dirty": False,
        "world_id": 0,
        "snapshot_id": "session-decision-frame:500",
        "seq": 500,
        "snapshot_seq": 120,
        "ts": "2026-08-30T09:00:00+00:00",
        "cycle": 250,
        "game": {"paused": True},
        "duplicants": {"total": 8},
        "resources": {"food_kcal": 200000.0},
        "alerts": [],
        "research": {"current": ""},
        "buildings": {},
        "quality": {},
        "bridge": {"stale": False, "snapshot_age_seconds": 1.0},
    }
    state_hash = canonical_state_hash(state)
    identity = {
        "session_id": state["session_id"],
        "snapshot_id": state["snapshot_id"],
        "state_hash": state_hash,
    }
    health = {
        "schema": "oni.bridge.health.v1",
        "status": "ok",
        "current_session_id": state["session_id"],
        "latest_snapshot_id": state["snapshot_id"],
        "latest_state_hash": state_hash,
        "source": dict(identity),
        "snapshot_age_seconds": 1.0,
        "trust_ready": True,
        "replay_mode": False,
        "session_start_observed": True,
    }
    if include_command_pipeline:
        health["command_pipeline"] = command_pipeline()
    advice = {
        "schema": "oni.advice.v2",
        "session_id": state["session_id"],
        "source": dict(identity),
        "items": [
            {
                "incident_id": "monitor_oxygen",
                "risk": "low",
                "incident_opened_at": state["ts"],
                "source_snapshot_id": state["snapshot_id"],
                "state_hash": state_hash,
                "title": "Monitor oxygen",
            }
        ],
    }
    incidents = {
        "schema": "oni.incidents.v1",
        "session_id": state["session_id"],
        "source": dict(identity),
        "items": [
            {
                "incident_id": "monitor_oxygen",
                "status": "open",
                "risk": "low",
                "snapshot_id": state["snapshot_id"],
                "state_hash": state_hash,
            }
        ],
        "timeline": [],
    }
    plan = {
        "schema": "oni.plan.v2",
        "source": dict(identity),
        "mode": "rule_only",
        "plan": [
            {
                "priority": 1,
                "incident_id": "monitor_oxygen",
                "risk": "low",
                "source_snapshot_id": state["snapshot_id"],
                "state_hash": state_hash,
                "allowed_action": "focus_note",
            }
        ],
        "llm": {"enabled": False, "called": False, "status": "disabled"},
    }
    objectives = [objective(gate_id) for gate_id in SUSTAINABILITY_GATE_IDS]
    objectives.append(objective("indefinite_survival", phase="sustain"))
    strategy = {
        "schema": "oni.strategy.v2",
        "mode": "deterministic",
        "auto_execute": False,
        "status": "ok",
        "operating_mode": "observe",
        "phase": {"id": "sustain", "order": 4, "confidence": "high", "reason": "fixture"},
        "readiness": {
            "status": "ready",
            "stable_window_verified": True,
            "required_consecutive_samples": 120,
            "observed_consecutive_samples": 120,
            "required_duration_seconds": 595.0,
            "observed_duration_seconds": 600.0,
            "required_game_cycles": 10.0,
            "observed_game_cycles": 10.0,
        },
        "session_id": state["session_id"],
        "snapshot_id": state["snapshot_id"],
        "snapshot_seq": state["snapshot_seq"],
        "seq": state["seq"],
        "ts": state["ts"],
        "state_hash": state_hash,
        "source": dict(identity),
        "phase_evidence": {
            "schema": "oni.phase-evidence.v1",
            "session_id": state["session_id"],
            "last_snapshot_id": state["snapshot_id"],
            "current_phase": "sustain",
            "evidence_window": {
                "schema": "oni.evidence-window.v1",
                "phase": "sustain",
                "session_id": state["session_id"],
                "start_seq": 300,
                "end_seq": state["seq"],
                "start_snapshot_seq": 1,
                "end_snapshot_seq": state["snapshot_seq"],
                "start_ts": "2026-08-30T08:50:00+00:00",
                "end_ts": state["ts"],
                "start_game_time_cycles": 240.0,
                "end_game_time_cycles": 250.0,
                "sample_count": 120,
                "gap_free": True,
                "input_hash": "sha256:" + "c" * 64,
                "critical_incidents": 0,
            },
        },
        "objectives": objectives,
        "next_actions": [
            {
                "id": "oxygen_security.note",
                "kind": "operator_recommendation",
                "action": "focus_note",
                "args": {"tag": "oxygen"},
                "risk": "low",
                "requires_approval": True,
                "auto_execute": False,
                "reason": "fixture",
            }
        ],
    }
    policy = {
        "schema": "oni.action-policy.v1",
        "source": dict(identity),
        "actions_enabled": actions_enabled,
        "suggestions_enabled": suggestions_enabled,
        "allowed_actions": ["focus_note"],
        "allowed_save_hashes": [SAVE_HASH] if actions_enabled else [],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
    }
    return {
        "state": state,
        "health": health,
        "advice": advice,
        "incidents": incidents,
        "plan": plan,
        "strategy": strategy,
        "policy": policy,
    }


def build(values: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return build_decision_frame(
        values["state"],
        values["health"],
        values["advice"],
        values["incidents"],
        values["plan"],
        values["strategy"],
        values["policy"],
    )


def rehash(frame: Dict[str, Any]) -> None:
    payload = {key: value for key, value in frame.items() if key != "frame_id"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    frame["frame_id"] = "sha256:" + hashlib.sha256(encoded).hexdigest()


def case_healthy_advisory() -> Dict[str, Any]:
    frame = build(healthy_inputs(actions_enabled=False, suggestions_enabled=True))
    validate_decision_frame(frame)
    readiness = frame["readiness"]
    require(frame["trust"]["status"] == "trusted", "healthy frame is not trusted")
    require(readiness["observe"]["ready"], "healthy observation is blocked")
    require(readiness["advise"]["ready"], "healthy advice is blocked")
    require(readiness["propose"]["ready"], "suggestion-enabled proposal is blocked")
    require(not readiness["advance"]["ready"], "disabled action policy allowed advancement")
    require(readiness["claim_sustainable"]["ready"], "disabled action policy blocked observational sustainability proof")
    require(blocker_codes(frame) == {"ACTIONS_DISABLED"}, "healthy advisory has unexpected blockers")
    recommendation = frame["projections"]["strategy"]["recommendations"][0]
    require("action" not in recommendation and "args" not in recommendation and "command" not in recommendation, "strategy recommendation became an executable command")
    return {"frame_id": frame["frame_id"], "readiness": readiness}


def case_stale_blocked() -> Dict[str, Any]:
    values = healthy_inputs()
    values["state"]["bridge"]["stale"] = True
    values["health"]["status"] = "stale"
    # Rebind every state-hash claim after the fixture state changes.
    state_hash = canonical_state_hash(values["state"])
    for name in ("health", "advice", "incidents", "plan", "strategy", "policy"):
        values[name]["source"]["state_hash"] = state_hash
    values["health"]["latest_state_hash"] = state_hash
    values["strategy"]["state_hash"] = state_hash
    for name, child_key in (("advice", "items"), ("incidents", "items"), ("plan", "plan")):
        for item in values[name][child_key]:
            item["state_hash"] = state_hash
    frame = build(values)
    codes = blocker_codes(frame)
    require({"STATE_STALE", "HEALTH_NOT_OK"} <= codes, "stale health blockers are not distinct")
    require(not any(entry["ready"] for entry in frame["readiness"].values()), "stale frame did not fail closed")
    return {"blockers": sorted(codes)}


def case_identity_mismatch_fail_closed() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True)
    values["advice"]["source"]["snapshot_id"] = "session-decision-frame:41"
    try:
        build(values)
    except DecisionFrameError:
        rejected = True
    else:
        rejected = False
    require(rejected, "identity mismatch produced a publishable frame")
    return {"mismatch_rejected": rejected}


def case_child_identity_missing() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True)
    del values["plan"]["source"]["state_hash"]
    frame = build(values)
    require("OUTPUT_IDENTITY_MISSING" in blocker_codes(frame), "missing child identity was not reported")
    require(not frame["trust"]["output_identity_complete"], "missing child identity was declared complete")
    require(not any(entry["ready"] for entry in frame["readiness"].values()), "missing child identity did not block every stage")
    validate_decision_frame(frame)
    return {"blockers": sorted(blocker_codes(frame)), "all_stages_blocked": True}


def case_source_identity_missing() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True)
    del values["state"]["save_id_hash"]
    state_hash = canonical_state_hash(values["state"])
    for name in ("health", "advice", "incidents", "plan", "strategy", "policy"):
        values[name]["source"]["state_hash"] = state_hash
    values["health"]["latest_state_hash"] = state_hash
    values["strategy"]["state_hash"] = state_hash
    for name, child_key in (("advice", "items"), ("incidents", "items"), ("plan", "plan")):
        for item in values[name][child_key]:
            item["state_hash"] = state_hash
    frame = build(values)
    require("SOURCE_IDENTITY_MISSING" in blocker_codes(frame), "missing state identity was not reported")
    require(not frame["trust"]["source_identity_complete"], "missing state identity was declared complete")
    require(not any(entry["ready"] for entry in frame["readiness"].values()), "missing state identity did not block every stage")
    validate_decision_frame(frame)
    return {"blockers": sorted(blocker_codes(frame)), "all_stages_blocked": True}


def case_strategy_not_ready() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True)
    values["strategy"]["status"] = "degraded"
    values["strategy"]["phase"] = {
        "id": "stabilize",
        "order": 1,
        "confidence": "low",
        "reason": "fixture",
    }
    values["strategy"]["readiness"] = {
        "status": "not_ready",
        "stable_window_verified": False,
        "required_consecutive_samples": 12,
        "observed_consecutive_samples": 2,
        "required_duration_seconds": 55.0,
        "observed_duration_seconds": 5.0,
        "required_game_cycles": 0.25,
        "observed_game_cycles": 0.01,
    }
    for item in values["strategy"]["objectives"]:
        if item["id"] == "power_resilience":
            item["status"] = "unknown"
        if item["id"] == "indefinite_survival":
            item["status"] = "deferred"
    frame = build(values)
    codes = blocker_codes(frame)
    require(frame["readiness"]["propose"]["ready"], "strategy readiness incorrectly blocked proposal generation")
    require(not frame["readiness"]["advance"]["ready"], "low-confidence strategy allowed advancement")
    require(not frame["readiness"]["claim_sustainable"]["ready"], "unproven strategy claimed sustainability")
    expected = {
        "STRATEGY_STATUS_NOT_READY",
        "STRATEGY_CONFIDENCE_LOW",
        "STRATEGY_WINDOW_NOT_VERIFIED",
        "STRATEGY_CRITICAL_GATE_NOT_PASSED",
        "STRATEGY_PHASE_NOT_SUSTAIN",
        "SUSTAINABILITY_GATE_NOT_PASSED",
        "INDEFINITE_SURVIVAL_NOT_PROVEN",
    }
    require(expected <= codes, "strategy blockers are incomplete")
    return {"blockers": sorted(codes)}


def case_replay_never_actionable() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True)
    values["health"]["replay_mode"] = True
    frame = build(values)
    codes = blocker_codes(frame)
    require("REPLAY_MODE" in codes, "replay mode was not made explicit")
    require(frame["readiness"]["observe"]["ready"], "replay blocked bounded observation")
    require(frame["readiness"]["advise"]["ready"], "replay blocked bounded advice")
    for stage in ("propose", "advance", "claim_sustainable"):
        require(not frame["readiness"][stage]["ready"], f"replay allowed {stage}")
    validate_decision_frame(frame)
    return {"blockers": sorted(codes), "actionable_stages_blocked": True}


def case_unverified_window_blocks_claim() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True)
    values["strategy"]["phase"]["confidence"] = "medium"
    values["strategy"]["readiness"] = {
        "status": "not_ready",
        "stable_window_verified": False,
        "required_consecutive_samples": 120,
        "observed_consecutive_samples": 119,
        "required_duration_seconds": 595.0,
        "observed_duration_seconds": 590.0,
        "required_game_cycles": 10.0,
        "observed_game_cycles": 9.9,
    }
    frame = build(values)
    codes = blocker_codes(frame)
    require("STRATEGY_WINDOW_NOT_VERIFIED" in codes, "unverified strategy window was not blocked")
    require(not frame["readiness"]["claim_sustainable"]["ready"], "119/120 samples claimed sustainability")
    validate_decision_frame(frame)
    return {"blockers": sorted(codes), "observed": 119, "required": 120}


def case_evidence_window_tampering_blocks_claim() -> Dict[str, Any]:
    mutations = {
        "input_hash": lambda strategy: strategy["phase_evidence"]["evidence_window"].__setitem__(
            "input_hash", "sha256:tampered"
        ),
        "incident": lambda strategy: strategy["phase_evidence"]["evidence_window"].__setitem__(
            "critical_incidents", 1
        ),
        "snapshot_sequence": lambda strategy: strategy["phase_evidence"]["evidence_window"].__setitem__(
            "end_snapshot_seq", 119
        ),
        "duration": lambda strategy: strategy["readiness"].__setitem__(
            "observed_duration_seconds", 594.0
        ),
    }
    rejected = []
    for name, mutate in mutations.items():
        values = healthy_inputs(actions_enabled=True)
        mutate(values["strategy"])
        frame = build(values)
        require("STRATEGY_INVALID" in blocker_codes(frame), f"{name} tamper left strategy valid")
        require(
            not frame["readiness"]["claim_sustainable"]["ready"],
            f"{name} tamper claimed sustainability",
        )
        validate_decision_frame(frame)
        rejected.append(name)
    return {"rejected": rejected}


def case_disabled_policy() -> Dict[str, Any]:
    frame = build(healthy_inputs(actions_enabled=False, suggestions_enabled=False))
    codes = blocker_codes(frame)
    require({"SUGGESTIONS_DISABLED", "ACTIONS_DISABLED"} <= codes, "policy gates are not distinguished")
    require(frame["readiness"]["observe"]["ready"] and frame["readiness"]["advise"]["ready"], "disabled policy blocked observation/advice")
    require(not frame["readiness"]["propose"]["ready"], "disabled suggestions allowed proposal")
    require(not frame["readiness"]["advance"]["ready"], "disabled actions allowed advancement")
    require(frame["readiness"]["claim_sustainable"]["ready"], "policy disabled observational claim")
    accepted_boundaries = []
    for ttl, maximum in ((1, 1), (300, 100)):
        values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
        values["policy"]["command_ttl_seconds"] = ttl
        values["policy"]["max_pending_commands"] = maximum
        bounded = build(values)
        require(bounded["projections"]["policy"]["valid"] is True, "valid policy bound was rejected")
        validate_decision_frame(bounded)
        accepted_boundaries.append({"command_ttl_seconds": ttl, "max_pending_commands": maximum})

    invalid_mutations: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "ttl_zero": lambda policy: policy.__setitem__("command_ttl_seconds", 0),
        "ttl_above_max": lambda policy: policy.__setitem__("command_ttl_seconds", 301),
        "ttl_string": lambda policy: policy.__setitem__("command_ttl_seconds", "30"),
        "ttl_boolean": lambda policy: policy.__setitem__("command_ttl_seconds", True),
        "pending_zero": lambda policy: policy.__setitem__("max_pending_commands", 0),
        "pending_above_max": lambda policy: policy.__setitem__("max_pending_commands", 101),
        "pending_string": lambda policy: policy.__setitem__("max_pending_commands", "20"),
        "pending_boolean": lambda policy: policy.__setitem__("max_pending_commands", False),
        "pending_missing": lambda policy: policy.pop("max_pending_commands"),
        "unknown_action": lambda policy: policy["allowed_actions"].append("teleport_duplicant"),
        "unknown_field": lambda policy: policy.__setitem__("optimistic", True),
        "naive_generated_at": lambda policy: policy.__setitem__(
            "generated_at", "2026-08-31T12:00:00"
        ),
    }
    rejected = []
    for name, mutate in invalid_mutations.items():
        values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
        mutate(values["policy"])
        invalid = build(values)
        require(invalid["projections"]["policy"]["valid"] is False, f"{name} policy was accepted")
        require(invalid["projections"]["policy"]["policy_hash"] is None, f"{name} retained authority hash")
        require("POLICY_INVALID" in blocker_codes(invalid), f"{name} omitted POLICY_INVALID")
        require(not invalid["readiness"]["propose"]["ready"], f"{name} allowed a proposal")
        validate_decision_frame(invalid)
        rejected.append(name)
    return {
        "blockers": sorted(codes),
        "accepted_boundaries": accepted_boundaries,
        "strict_policy_rejections": sorted(rejected),
    }


def case_command_pipeline_blocks_mutation_only() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    values["health"]["command_pipeline"] = command_pipeline(
        proposal_blockers=["mutation_confirmation_in_flight:cmd-fixture"],
        candidate_ids=["cmd-fixture"],
    )
    frame = build(values)
    validate_decision_frame(frame)
    pipeline = frame["projections"]["health"]["command_pipeline"]
    codes = blocker_codes(frame)
    require(codes == {"COMMAND_PIPELINE_MUTATION_BLOCKED"}, "pipeline blocker code drifted")
    require(frame["trust"]["status"] == "trusted", "queue occupancy contaminated state trust")
    require(frame["readiness"]["observe"]["ready"], "queue occupancy blocked observation")
    require(frame["readiness"]["advise"]["ready"], "queue occupancy blocked advice")
    require(not frame["readiness"]["propose"]["ready"], "occupied pipeline allowed proposal")
    require(not frame["readiness"]["advance"]["ready"], "occupied pipeline allowed advance")
    require(frame["readiness"]["claim_sustainable"]["ready"], "queue occupancy blocked observation-only claim")
    confirmation = pipeline["readiness"]["mutation_confirmation"]
    require(confirmation["ready"] is True, "proposal blocker disabled confirmation recovery")
    require(confirmation["candidate_ids"] == ["cmd-fixture"], "confirmation candidate was not projected")
    return {
        "decision_blocker": "COMMAND_PIPELINE_MUTATION_BLOCKED",
        "proposal_blockers": pipeline["readiness"]["mutation_proposal"]["blockers"],
        "confirmation_ready": confirmation["ready"],
    }


def case_command_pipeline_invalid_fail_closed() -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    values["health"]["command_pipeline"]["unexpected"] = True
    frame = build(values)
    validate_decision_frame(frame)
    pipeline = frame["projections"]["health"]["command_pipeline"]
    require(pipeline["valid"] is False, "malformed Bridge pipeline was accepted")
    require(
        blocker_codes(frame) == {"COMMAND_PIPELINE_INVALID"},
        "malformed Bridge pipeline blocker code drifted",
    )
    require(frame["trust"]["status"] == "trusted", "invalid mutation gate contaminated state trust")
    require(frame["readiness"]["observe"]["ready"], "invalid mutation gate blocked observation")
    require(not frame["readiness"]["propose"]["ready"], "invalid mutation gate allowed proposal")
    require(not frame["readiness"]["advance"]["ready"], "invalid mutation gate allowed advance")
    require(
        pipeline["readiness"]["mutation_confirmation"]["ready"] is False,
        "invalid mutation gate optimistically enabled confirmation",
    )
    blocker = next(item for item in frame["blockers"] if item["code"] == "COMMAND_PIPELINE_INVALID")
    require(
        blocker == {"code": "COMMAND_PIPELINE_INVALID", "source": "health", "field": "command_pipeline"},
        "invalid pipeline blocker tuple is nondeterministic",
    )
    return {"projection_valid": False, "blocker": blocker, "proposal_ready": False}


def case_command_pipeline_projection_strict() -> Dict[str, Any]:
    frame = build(healthy_inputs(actions_enabled=True, suggestions_enabled=True))
    tampered_count = copy.deepcopy(frame)
    tampered_count["projections"]["health"]["command_pipeline"]["counts"]["pending"] = True
    rehash(tampered_count)
    try:
        validate_decision_frame(tampered_count)
    except DecisionFrameError:
        boolean_count_rejected = True
    else:
        boolean_count_rejected = False
    require(boolean_count_rejected, "strict pipeline projection accepted a boolean count")

    tampered_keys = copy.deepcopy(frame)
    tampered_keys["projections"]["health"]["command_pipeline"]["extra"] = "optimistic"
    rehash(tampered_keys)
    try:
        validate_decision_frame(tampered_keys)
    except DecisionFrameError:
        unknown_key_rejected = True
    else:
        unknown_key_rejected = False
    require(unknown_key_rejected, "strict pipeline projection accepted an unknown key")

    hidden_confirmation = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    hidden_confirmation["health"]["command_pipeline"] = command_pipeline(
        confirmation_blockers=["command_json_invalid:confirming:invalid.json"]
    )
    hidden_confirmation_frame = build(hidden_confirmation)
    require(
        blocker_codes(hidden_confirmation_frame) == {"COMMAND_PIPELINE_INVALID"},
        "confirmation-only blocker did not fail the whole mutation gate closed",
    )
    return {
        "boolean_count_rejected": boolean_count_rejected,
        "unknown_key_rejected": unknown_key_rejected,
        "hidden_confirmation_blocker_rejected": True,
    }


def case_legacy_health_without_command_pipeline() -> Dict[str, Any]:
    frame = build(
        healthy_inputs(
            actions_enabled=True,
            suggestions_enabled=True,
            include_command_pipeline=False,
        )
    )
    validate_decision_frame(frame)
    require(
        "command_pipeline" not in frame["projections"]["health"],
        "legacy health fabricated a command-pipeline claim",
    )
    require(
        not (blocker_codes(frame) & {"COMMAND_PIPELINE_INVALID", "COMMAND_PIPELINE_MUTATION_BLOCKED"}),
        "legacy health was blocked by an absent optional gate",
    )
    return {"legacy_absence_accepted": True, "proposal_ready": frame["readiness"]["propose"]["ready"]}


def case_determinism_and_immutability() -> Dict[str, Any]:
    values = healthy_inputs()
    before = copy.deepcopy(values)
    first = build(values)
    second = build(values)
    require(first == second and first["frame_id"] == second["frame_id"], "frame generation is nondeterministic")
    require(values == before, "builder modified an input")

    values["advice"]["items"][0]["risk"] = "high"
    require(first["projections"]["advice"]["items"][0]["risk"] == "low", "frame aliases caller input")
    first["projections"]["advice"]["items"][0]["risk"] = "high"
    require(before["advice"]["items"][0]["risk"] == "low", "caller input aliases frame output")
    try:
        validate_decision_frame(first)
    except DecisionFrameError:
        mutation_detected = True
    else:
        mutation_detected = False
    require(mutation_detected, "frame hash did not detect post-build mutation")
    return {"stable_frame_id": second["frame_id"], "mutation_detected": mutation_detected}


def case_no_secret_or_path_leak() -> Dict[str, Any]:
    values = healthy_inputs()
    secret = "sk-fixtureSuperSecret123456"
    windows_path = "C:\\Users\\fixture\\secrets\\token.txt"
    posix_path = "/home/fixture/secrets/token.txt"
    values["health"]["warnings"] = [secret, windows_path]
    values["advice"]["items"][0].update(
        {"reason": f"read {windows_path}", "command": {"action": "focus_note", "args": {"token": secret}}}
    )
    values["incidents"]["items"][0]["reason"] = posix_path
    values["plan"]["plan"][0]["rationale"] = secret
    values["plan"]["provider_config"] = {"api_key": secret, "cache": windows_path}
    values["strategy"]["goal"] = f"do not copy {secret} or {posix_path}"
    values["strategy"]["next_actions"][0]["args"] = {"path": windows_path, "secret": secret}
    values["policy"]["debug_path"] = posix_path
    frame = build(values)
    encoded = json.dumps(frame, ensure_ascii=False, sort_keys=True)
    for forbidden in (secret, windows_path, posix_path, "provider_config", "debug_path"):
        require(forbidden not in encoded, f"decision frame leaked {forbidden!r}")
    require('"command"' not in encoded and '"args"' not in encoded, "decision frame copied executable material")
    validate_decision_frame(frame)
    return {"serialized_bytes": len(encoded.encode("utf-8")), "redacted_by_projection": True}


def case_strict_schema() -> Dict[str, Any]:
    frame = build(healthy_inputs())
    tampered = copy.deepcopy(frame)
    tampered["unexpected"] = True
    try:
        validate_decision_frame(tampered)
    except DecisionFrameError:
        rejected_unknown_key = True
    else:
        rejected_unknown_key = False
    require(rejected_unknown_key, "strict validator accepted an unknown top-level key")
    legacy_frame = copy.deepcopy(frame)
    legacy_frame["schema"] = "oni.decision-frame.v1"
    try:
        validate_decision_frame(legacy_frame)
    except DecisionFrameError:
        rejected_legacy_schema = True
    else:
        rejected_legacy_schema = False
    require(rejected_legacy_schema, "v2 validator silently accepted a v1 frame")

    missing_snapshot_seq = copy.deepcopy(frame)
    del missing_snapshot_seq["source"]["snapshot_seq"]
    rehash(missing_snapshot_seq)
    try:
        validate_decision_frame(missing_snapshot_seq)
    except DecisionFrameError:
        rejected_missing_snapshot_seq = True
    else:
        rejected_missing_snapshot_seq = False
    require(rejected_missing_snapshot_seq, "frame source without snapshot_seq was accepted")

    future_snapshot_seq = copy.deepcopy(frame)
    future_snapshot_seq["source"]["snapshot_seq"] = future_snapshot_seq["source"]["seq"] + 1
    rehash(future_snapshot_seq)
    try:
        validate_decision_frame(future_snapshot_seq)
    except DecisionFrameError:
        rejected_future_snapshot_seq = True
    else:
        rejected_future_snapshot_seq = False
    require(rejected_future_snapshot_seq, "frame source snapshot_seq beyond seq was accepted")

    naive_lifecycle = healthy_inputs()
    naive_lifecycle["advice"]["items"][0]["incident_opened_at"] = "2026-08-30T09:00:00"
    naive_frame = build(naive_lifecycle)
    require(
        "ADVICE_INVALID" in blocker_codes(naive_frame)
        and naive_frame["readiness"]["propose"]["ready"] is False,
        "timezone-naive advice lifecycle did not fail closed",
    )

    future_lifecycle = healthy_inputs()
    future_lifecycle["advice"]["items"][0]["incident_opened_at"] = "2099-01-01T00:00:00+00:00"
    try:
        build(future_lifecycle)
    except DecisionFrameError:
        rejected_future_lifecycle = True
    else:
        rejected_future_lifecycle = False
    require(rejected_future_lifecycle, "future advice lifecycle was accepted")
    compatibility = healthy_inputs()
    del compatibility["strategy"]["source"]
    legacy_strategy_frame = build(compatibility)
    require(legacy_strategy_frame["trust"]["output_identity_complete"], "legacy top-level strategy identity was not accepted")
    return {
        "unknown_key_rejected": rejected_unknown_key,
        "legacy_v1_rejected": rejected_legacy_schema,
        "missing_snapshot_seq_rejected": rejected_missing_snapshot_seq,
        "future_snapshot_seq_rejected": rejected_future_snapshot_seq,
        "naive_lifecycle_blocked": True,
        "future_lifecycle_rejected": rejected_future_lifecycle,
        "legacy_strategy_identity_accepted": True,
    }


def main() -> int:
    cases: Iterable[tuple[str, Any]] = (
        ("healthy_advisory", case_healthy_advisory),
        ("stale_blocked", case_stale_blocked),
        ("identity_mismatch_fail_closed", case_identity_mismatch_fail_closed),
        ("child_identity_missing", case_child_identity_missing),
        ("source_identity_missing", case_source_identity_missing),
        ("strategy_not_ready", case_strategy_not_ready),
        ("replay_never_actionable", case_replay_never_actionable),
        ("unverified_window_blocks_claim", case_unverified_window_blocks_claim),
        ("evidence_window_tampering", case_evidence_window_tampering_blocks_claim),
        ("disabled_policy", case_disabled_policy),
        ("command_pipeline_blocks_mutation_only", case_command_pipeline_blocks_mutation_only),
        ("command_pipeline_invalid_fail_closed", case_command_pipeline_invalid_fail_closed),
        ("command_pipeline_projection_strict", case_command_pipeline_projection_strict),
        ("legacy_health_without_command_pipeline", case_legacy_health_without_command_pipeline),
        ("determinism_and_immutability", case_determinism_and_immutability),
        ("no_secret_or_path_leak", case_no_secret_or_path_leak),
        ("strict_schema", case_strict_schema),
    )
    results: Dict[str, Any] = {}
    try:
        for name, function in cases:
            results[name] = function()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": "oni.decision-frame-verification.v1",
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "cases": results,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "schema": "oni.decision-frame-verification.v1",
                "status": "passed",
                "case_count": len(results),
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
