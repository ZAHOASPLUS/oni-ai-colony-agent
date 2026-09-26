#!/usr/bin/env python3
"""Offline contract verification for ``oni.control-packet.v1``."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import tempfile
import sys
from typing import Any, Callable, Dict, Mapping


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.canonical_hash import canonical_hash  # noqa: E402
from agent.control_packet import (  # noqa: E402
    ControlPacketError,
    build_control_packet,
    validate_control_packet,
)
from agent.decision import build_decision_frame  # noqa: E402
from agent.command_queue import write_json_atomic  # noqa: E402
from scripts.verify_decision_frame import build as build_fixture_frame  # noqa: E402
from scripts.verify_decision_frame import healthy_inputs  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def frame(*, actions: bool = True, suggestions: bool = True, replay: bool = False) -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=actions, suggestions_enabled=suggestions)
    values["health"]["replay_mode"] = replay
    return build_fixture_frame(values)


def candidate(command_id: str = "cmd-1111111111111111", *, intent: str | None = None) -> Dict[str, Any]:
    return {
        "command_id": command_id,
        "action": "set_paused",
        "intent_hash": intent or ("sha256:" + "7" * 64),
        "risk": "low",
        "requires_approval": True,
        "auto_execute": False,
    }


def gate(
    *,
    candidates: list[Dict[str, Any]] | None = None,
    blockers: list[str] | None = None,
    duplicate: bool = False,
    outcome_unknown: bool = False,
    stale: bool = False,
    in_flight: bool = False,
    invalid_read: bool = False,
    source_frame: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    bound_frame = source_frame or frame()
    pending_candidates = sorted(
        copy.deepcopy(candidates or []), key=lambda item: (item["command_id"], item["intent_hash"])
    )
    proposal_blockers = sorted(set(blockers or []))
    confirmation_blockers = ["mutation_outcome_unknown_unresolved:cmd-unknown"] if outcome_unknown else []
    if outcome_unknown:
        proposal_blockers = sorted(set(proposal_blockers + confirmation_blockers))
    commands: list[Dict[str, Any]] = [
        {
            "command_id": item["command_id"],
            "action": item["action"],
            "stage": "pending",
            "status": "pending_human_approval",
            "classification": "pending_proposal",
            "mutating": True,
            "path": f"commands/pending/{item['command_id']}.json",
        }
        for item in pending_candidates
    ]
    if in_flight:
        commands.append(
            {
                "command_id": "cmd-inflight",
                "action": "set_paused",
                "stage": "approving",
                "status": "approval_claimed",
                "classification": "approval_claimed",
                "mutating": True,
                "path": "commands/approving/cmd-inflight.json",
            }
        )
    if outcome_unknown:
        commands.append(
            {
                "command_id": "cmd-unknown",
                "action": "set_paused",
                "stage": "failed",
                "status": "failed",
                "classification": "outcome_unknown",
                "mutating": True,
                "path": "commands/failed/cmd-unknown.json",
            }
        )
    commands.sort(key=lambda item: (item["command_id"], item["stage"], item["path"]))
    in_flight_records = [
        copy.deepcopy(item)
        for item in commands
        if item["mutating"] and item["stage"] in {"approving", "approved", "executing", "confirming"}
    ]
    return {
        "schema": "oni.command-pipeline-gate.v1",
        "evaluation_context": {
            "phase": "frame_bound",
            "proposal_frame_id": bound_frame["frame_id"],
            "state_hash": bound_frame["source"]["state_hash"],
            "policy_hash": bound_frame["projections"]["policy"]["policy_hash"],
        },
        "ready": not proposal_blockers,
        "blockers": proposal_blockers,
        "readiness": {
            "mutation_proposal": {"ready": not proposal_blockers, "blockers": proposal_blockers},
            "mutation_confirmation": {
                "ready": not confirmation_blockers,
                "blockers": confirmation_blockers,
                "candidate_ids": [],
            },
        },
        "counts": {
            "pending": len(pending_candidates),
            "approving": 1 if in_flight else 0,
            "approved": 0,
            "executing": 0,
            "confirming": 0,
            "failed": 1 if outcome_unknown else 0,
        },
        "commands": commands,
        "pending_candidates": pending_candidates,
        "in_flight_mutations": in_flight_records,
        "ambiguous_outcomes": (
            [
                {
                    "command_id": "cmd-unknown",
                    "classification": "outcome_unknown",
                    "stages": ["failed"],
                    "mutating": True,
                }
            ]
            if outcome_unknown
            else []
        ),
        "duplicate_ids": ([{"command_id": "cmd-duplicate"}] if duplicate else []),
        "stale_pending": ([{"command_id": "cmd-stale"}] if stale else []),
        "invalid_records": ([{"reason": "command_json_invalid"}] if invalid_read else []),
    }


def action_gate(
    item: Dict[str, Any] | None = None,
    *,
    source_frame: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    selected = copy.deepcopy(item or candidate())
    return gate(
        candidates=[selected],
        blockers=[f"mutation_pending_selection:{selected['command_id']}"],
        source_frame=source_frame,
    )


def rehash(packet: Dict[str, Any]) -> None:
    packet["packet_id"] = canonical_hash(
        {key: value for key, value in packet.items() if key != "packet_id"}
    )


def rejected(packet: Mapping[str, Any], source_frame: Mapping[str, Any], source_gate: Mapping[str, Any]) -> bool:
    try:
        validate_control_packet(packet, source_frame, source_gate)
    except ControlPacketError:
        return True
    return False


def case_determinism_and_reorder() -> Dict[str, Any]:
    source_frame = frame()
    source_gate = gate()
    reordered_gate = dict(reversed(list(source_gate.items())))
    first = build_control_packet(source_frame, source_gate)
    second = build_control_packet(copy.deepcopy(source_frame), reordered_gate)
    require(first == second, "mapping key reorder changed the control packet")
    require(first == build_control_packet(source_frame, source_gate), "repeat build is nondeterministic")
    return {"packet_id": first["packet_id"], "mapping_reorder_stable": True}


def case_exact_keys_and_variants() -> Dict[str, Any]:
    source_frame = frame()
    action = build_control_packet(source_frame, action_gate())
    require(action["next_step"]["kind"] == "action_candidate", "action variant was not selected")

    operator = build_control_packet(source_frame, gate())
    require(
        operator["next_step"]["kind"] == "operator_recommendation",
        "operator recommendation variant was not selected",
    )
    observe_gate = gate(
        blockers=["mutation_outcome_unknown_unresolved:cmd-unknown"], outcome_unknown=True
    )
    observe = build_control_packet(source_frame, observe_gate)
    require(observe["next_step"]["kind"] == "observe_request", "observe variant was not selected")

    for name, blocker_value in (
        ("operator_pending", "mutation_pending_operator:cmd-operator"),
        ("invalid_mode", "mutation_pending_mode_invalid:cmd-invalid-mode"),
    ):
        blocked = build_control_packet(source_frame, gate(blockers=[blocker_value]))
        require(
            blocked["next_step"] == {
                "kind": "observe_request",
                "source": "pipeline_blocker",
                "identifier": blocker_value,
            },
            f"{name} latch was hidden by a recommendation",
        )
    blocked_frame = frame(actions=False)
    frame_blocked = build_control_packet(blocked_frame, gate(source_frame=blocked_frame))
    require(
        frame_blocked["next_step"] is not None
        and frame_blocked["next_step"]["kind"] == "operator_recommendation",
        "ACTIONS_DISABLED suppressed a non-executing recommendation",
    )
    replay_frame = frame(replay=True)
    replay_blocked = build_control_packet(replay_frame, gate(source_frame=replay_frame))
    require(
        replay_blocked["next_step"] is not None
        and replay_blocked["next_step"]["kind"] == "observe_request"
        and replay_blocked["next_step"]["identifier"] == "REPLAY_MODE",
        "non-advisory frame blocker was hidden by a recommendation",
    )
    replay_and_disabled_frame = frame(actions=False, replay=True)
    replay_and_disabled = build_control_packet(
        replay_and_disabled_frame, gate(source_frame=replay_and_disabled_frame)
    )
    require(
        replay_and_disabled["next_step"] is not None
        and replay_and_disabled["next_step"]["kind"] == "observe_request"
        and replay_and_disabled["next_step"]["identifier"] == "REPLAY_MODE",
        "alphabetical ACTIONS_DISABLED obscured replay-mode evidence",
    )

    strategy_values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    strategy_values["advice"]["items"] = []
    strategy_values["plan"]["plan"] = []
    template = copy.deepcopy(strategy_values["strategy"]["next_actions"][0])
    low = copy.deepcopy(template)
    low.update({"id": "a_low", "risk": "low"})
    high = copy.deepcopy(template)
    high.update({"id": "z_high", "risk": "high"})
    strategy_values["strategy"]["next_actions"] = [low, high]
    strategy_frame = build_fixture_frame(strategy_values)
    strategy_packet = build_control_packet(
        strategy_frame, gate(source_frame=strategy_frame)
    )
    require(
        strategy_packet["next_step"] is not None
        and strategy_packet["next_step"]["source"] == "strategy"
        and strategy_packet["next_step"]["identifier"] == "z_high",
        "strategy fallback ignored risk priority",
    )

    objective_values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    objective_values["advice"]["items"] = []
    objective_values["plan"]["plan"] = []
    objective_values["strategy"]["next_actions"] = []
    for objective in objective_values["strategy"]["objectives"]:
        objective["status"] = "satisfied"
        if objective["id"] == "food_reserve":
            objective.update({"status": "unknown", "priority": 95})
        elif objective["id"] == "oxygen_security":
            objective.update({"status": "unknown", "priority": 100})
    objective_frame = build_fixture_frame(objective_values)
    objective_packet = build_control_packet(
        objective_frame, gate(source_frame=objective_frame)
    )
    require(
        objective_packet["next_step"]
        == {
            "kind": "observe_request",
            "source": "strategy_objective",
            "identifier": "oxygen_security",
        },
        "survival objective priority was replaced by identifier ordering",
    )

    for packet in (action, operator, observe):
        require(
            set(packet) == {"schema", "packet_id", "mode", "source", "pipeline", "next_step"},
            "top-level keys drifted",
        )
        require(packet["mode"] == "non_executing", "packet became executing")
    tampered = copy.deepcopy(operator)
    tampered["extra"] = True
    rehash(tampered)
    require(rejected(tampered, source_frame, gate()), "extra top-level key was accepted")
    return {
        "variants": ["action_candidate", "operator_recommendation", "observe_request"],
        "blocker_priority": ["operator_pending", "invalid_mode", "REPLAY_MODE"],
        "actions_disabled_recommendation": True,
        "strategy_risk_priority": "high",
        "objective_priority": "oxygen_security",
    }


def case_binding_tamper() -> Dict[str, Any]:
    source_frame = frame()
    source_gate = action_gate()
    packet = build_control_packet(source_frame, source_gate)
    mutations: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "frame": lambda value: value["source"].__setitem__("frame_id", "sha256:" + "1" * 64),
        "state": lambda value: value["source"].__setitem__("state_hash", "sha256:" + "2" * 64),
        "snapshot_sequence": lambda value: value["source"].__setitem__(
            "snapshot_seq", value["source"]["snapshot_seq"] - 1
        ),
        "policy": lambda value: value["source"]["policy"].__setitem__("policy_hash", "sha256:" + "3" * 64),
        "gate": lambda value: value["pipeline"].__setitem__("gate_hash", "sha256:" + "4" * 64),
        "intent": lambda value: value["next_step"].__setitem__("intent_hash", "sha256:" + "5" * 64),
    }
    rejected_names: list[str] = []
    for name, mutate in mutations.items():
        tampered = copy.deepcopy(packet)
        mutate(tampered)
        rehash(tampered)
        require(rejected(tampered, source_frame, source_gate), f"{name} tamper was accepted")
        rejected_names.append(name)

    tampered_frame = copy.deepcopy(source_frame)
    tampered_frame["source"]["seq"] += 1
    require(rejected(packet, tampered_frame, source_gate), "tampered decision frame was accepted")
    tampered_gate = copy.deepcopy(source_gate)
    tampered_gate["counts"]["pending"] += 1
    require(rejected(packet, source_frame, tampered_gate), "tampered post-sync gate was accepted")

    context_tampers: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "pre_frame": lambda value: value["evaluation_context"].update(
            {"phase": "pre_frame", "proposal_frame_id": None}
        ),
        "wrong_frame": lambda value: value["evaluation_context"].__setitem__(
            "proposal_frame_id", "sha256:" + "8" * 64
        ),
        "wrong_state": lambda value: value["evaluation_context"].__setitem__(
            "state_hash", "sha256:" + "9" * 64
        ),
        "wrong_policy": lambda value: value["evaluation_context"].__setitem__(
            "policy_hash", "sha256:" + "a" * 64
        ),
    }
    for name, mutate in context_tampers.items():
        raw_gate = copy.deepcopy(source_gate)
        mutate(raw_gate)
        try:
            build_control_packet(source_frame, raw_gate)
        except ControlPacketError:
            pass
        else:
            raise AssertionError(f"{name} evaluation context was accepted")
    gate_contract_tampers: Dict[str, Callable[[Dict[str, Any]], None]] = {
        # An action gate carries its own selection blocker and is therefore
        # not ready.  Flipping only the top-level summary must be rejected
        # even though the nested proposal entry remains self-consistent.
        "top_level_ready": lambda value: value.__setitem__("ready", True),
        "pending_count": lambda value: value["counts"].__setitem__("pending", 0),
        "extra_top_level": lambda value: value.__setitem__("forged", True),
        "commands_type": lambda value: value.__setitem__("commands", "forged"),
        "candidate_record": lambda value: value["commands"][0].__setitem__(
            "command_id", "cmd-unrelated"
        ),
        "candidate_status": lambda value: value["commands"][0].__setitem__(
            "status", "approved"
        ),
        "candidate_path": lambda value: value["commands"][0].__setitem__(
            "path", "commands/pending/renamed-safe.json"
        ),
    }

    def confirmation_record_without_candidate_id(value: Dict[str, Any]) -> None:
        value["counts"]["confirming"] = 1
        value["commands"].append(
            {
                "command_id": "cmd-confirming",
                "action": "set_paused",
                "stage": "confirming",
                "status": "executed_pending_snapshot",
                "classification": "awaiting_snapshot_confirmation",
                "mutating": True,
                "path": "commands/confirming/cmd-confirming.json",
            }
        )
        value["commands"].sort(
            key=lambda item: (item["command_id"], item["stage"], item["path"])
        )
        value["in_flight_mutations"] = [
            copy.deepcopy(item)
            for item in value["commands"]
            if item["mutating"]
            and item["stage"] in {"approving", "approved", "executing", "confirming"}
        ]

    gate_contract_tampers["confirmation_candidate_ids"] = (
        confirmation_record_without_candidate_id
    )
    for name, mutate in gate_contract_tampers.items():
        raw_gate = copy.deepcopy(source_gate)
        mutate(raw_gate)
        try:
            build_control_packet(source_frame, raw_gate)
        except ControlPacketError:
            pass
        else:
            raise AssertionError(f"{name} gate contract tamper was accepted")
    return {
        "rejected": rejected_names
        + ["bound_frame", "bound_gate"]
        + sorted(context_tampers)
        + sorted(gate_contract_tampers)
    }


def case_forbidden_material() -> Dict[str, Any]:
    source_frame = frame()
    source_gate = gate()
    base = build_control_packet(source_frame, source_gate)
    probes = {
        "args": lambda value: value["next_step"].__setitem__("args", {}),
        "body": lambda value: value["next_step"].__setitem__("body", "x"),
        "path": lambda value: value["next_step"].__setitem__("identifier", "C:\\Users\\fixture\\token"),
        "secret": lambda value: value["next_step"].__setitem__("identifier", "sk-fixtureSecret12345"),
    }
    for name, mutate in probes.items():
        value = copy.deepcopy(base)
        mutate(value)
        rehash(value)
        require(rejected(value, source_frame, source_gate), f"{name} material was accepted")
    return {"rejected": sorted(probes)}


def case_zero_one_and_ambiguity() -> Dict[str, Any]:
    source_frame = frame()
    zero = build_control_packet(source_frame, gate(invalid_read=True))
    require(zero["next_step"] is None, "structured invalid read did not publish null")
    one = build_control_packet(source_frame, action_gate())
    require(one["next_step"]["kind"] == "action_candidate", "unique candidate was not selected")

    first = candidate("cmd-1111111111111111", intent="sha256:" + "1" * 64)
    second = candidate("cmd-2222222222222222", intent="sha256:" + "2" * 64)
    multi_gate = gate(
        candidates=[second, first],
        blockers=[
            f"mutation_pending_selection:{first['command_id']}",
            f"mutation_pending_selection:{second['command_id']}",
        ],
    )
    duplicate_gate = gate(
        candidates=[first],
        blockers=[f"duplicate_command_id:{first['command_id']}"],
        duplicate=True,
    )
    unknown_gate = gate(
        candidates=[first],
        blockers=["mutation_outcome_unknown_unresolved:cmd-unknown"],
        outcome_unknown=True,
    )
    inflight_gate = gate(
        candidates=[first],
        blockers=["mutation_approval_claimed:cmd-inflight"],
        in_flight=True,
    )
    for name, source_gate in (
        ("multiple", multi_gate),
        ("duplicate", duplicate_gate),
        ("outcome_unknown", unknown_gate),
        ("in_flight", inflight_gate),
    ):
        packet = build_control_packet(source_frame, source_gate)
        require(
            packet["next_step"] is None or packet["next_step"]["kind"] != "action_candidate",
            f"{name} ambiguity selected an action candidate",
        )

    hidden_second = action_gate(first)
    hidden_second["commands"].append(
        {
            "command_id": second["command_id"],
            "action": second["action"],
            "stage": "pending",
            "status": "pending_human_approval",
            "classification": "pending_proposal",
            "mutating": True,
            "path": f"commands/pending/{second['command_id']}.json",
        }
    )
    hidden_second["commands"].sort(
        key=lambda item: (item["command_id"], item["stage"], item["path"])
    )
    hidden_second["counts"]["pending"] = 2
    hidden_second_packet = build_control_packet(source_frame, hidden_second)
    require(
        hidden_second_packet["next_step"] is None
        or hidden_second_packet["next_step"]["kind"] != "action_candidate",
        "a hidden second mutating pending record preserved an action candidate",
    )

    hidden_unknown = action_gate(first)
    hidden_unknown["commands"].append(
        {
            "command_id": "cmd-hidden-unknown",
            "action": "set_paused",
            "stage": "failed",
            "status": "failed",
            "classification": "outcome_unknown",
            "mutating": True,
            "path": "commands/failed/cmd-hidden-unknown.json",
        }
    )
    hidden_unknown["commands"].sort(
        key=lambda item: (item["command_id"], item["stage"], item["path"])
    )
    hidden_unknown["counts"]["failed"] = 1
    try:
        build_control_packet(source_frame, hidden_unknown)
    except ControlPacketError:
        hidden_unknown_rejected = True
    else:
        hidden_unknown_rejected = False
    require(hidden_unknown_rejected, "a hidden outcome_unknown record passed the gate contract")
    return {
        "zero_or_one": True,
        "ambiguities_fail_closed": 4,
        "hidden_second_mutation_suppressed": True,
        "hidden_outcome_unknown_rejected": True,
    }


def case_replay_disabled_and_latest_none() -> Dict[str, Any]:
    stale_values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    stale_values["health"]["status"] = "stale"
    stale_values["health"]["trust_ready"] = False
    untrusted_values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    untrusted_values["health"]["status"] = "untrusted_snapshot"
    untrusted_values["health"]["trust_ready"] = False
    cases = {
        "replay": frame(replay=True),
        "actions_disabled": frame(actions=False),
        "suggestions_disabled": frame(suggestions=False),
        "stale": build_fixture_frame(stale_values),
        "untrusted": build_fixture_frame(untrusted_values),
        "latest_none": build_decision_frame(None, None, None, None, None, None, None),
    }
    for name, source_frame in cases.items():
        source_gate = action_gate(source_frame=source_frame)
        packet = build_control_packet(source_frame, source_gate)
        require(
            packet["next_step"] is None or packet["next_step"]["kind"] != "action_candidate",
            f"{name} produced an action candidate",
        )
    return {"action_candidate_suppressed": sorted(cases)}


def run_case(name: str, callback: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return {"name": name, "status": "passed", "details": callback()}
    except Exception as exc:  # noqa: BLE001 - verifier must report all isolated cases
        return {"name": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def verify(output: pathlib.Path) -> Dict[str, Any]:
    cases: list[tuple[str, Callable[[], Dict[str, Any]]]] = [
        ("determinism_and_reorder", case_determinism_and_reorder),
        ("exact_keys_and_variants", case_exact_keys_and_variants),
        ("binding_tamper", case_binding_tamper),
        ("forbidden_material", case_forbidden_material),
        ("zero_one_and_ambiguity", case_zero_one_and_ambiguity),
        ("replay_disabled_and_latest_none", case_replay_disabled_and_latest_none),
    ]
    results = [run_case(name, callback) for name, callback in cases]
    passed = all(item["status"] == "passed" for item in results)
    report = {
        "schema": "oni.control-packet-verification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "summary": {
            "total": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "failed": sum(item["status"] != "passed" for item in results),
        },
        "cases": results,
    }
    write_json_atomic(output.expanduser().resolve(), report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path(tempfile.gettempdir()) / "oni-control-packet-verification.json",
    )
    args = parser.parse_args()
    report = verify(args.output)
    for item in report["cases"]:
        detail = item.get("details") if item["status"] == "passed" else item.get("error")
        print(f"[{item['status'].upper()}] {item['name']}: {json.dumps(detail, ensure_ascii=False, sort_keys=True)}")
    print(f"Report: {args.output.expanduser().resolve()}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
