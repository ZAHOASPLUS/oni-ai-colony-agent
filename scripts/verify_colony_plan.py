#!/usr/bin/env python3
"""Verify Batch D colony-plan state, approval, and recovery contracts."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import pathlib
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.advanced_actions import build_duplicant_priority_hint
from agent.action_outcomes import build_action_outcomes, commit_confirmed_outcomes
from agent.approval_receipt import build_approved_command, canonical_hash
from agent.colony_plan import (
    PLAN_DEFINITION_SCHEMA,
    ColonyPlanError,
    _quarantine_pending_command,
    abort_colony_plan,
    advance_colony_plan,
    create_colony_plan,
    load_active_plan,
    pause_colony_plan,
    recover_colony_plan,
    resume_colony_plan,
)
from agent.command_queue import ensure_command_dirs, write_json_atomic
from agent.decision import build_decision_frame
from agent.decision_frame_archive import (
    load_archived_decision_frame,
    persist_decision_frame_archive,
)
from agent.field_contracts import (
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.state_quality import canonical_state_hash
from scripts.verify_decision_frame import command_pipeline, healthy_inputs


DEFAULT_BRIDGE = pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def state(*, priority: int = 2, session_id: str = "session-plan-fixture", seq: int = 1) -> Dict[str, Any]:
    path = "duplicants.by_instance_id.id_123.chore_priorities.Dig"
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": iso_now(),
        "session_id": session_id,
        "seq": seq,
        "snapshot_seq": seq,
        "snapshot_id": f"{session_id}:{seq}",
        "game_build": "fixture-build",
        "mod_version": "fixture-version",
        "save_id_hash": "sha256:" + "a" * 64,
        "save_dirty": False,
        "world_id": 0,
        "observation_scope": {
            "active_world_id": 0,
            "populated_world_ids": [0],
            "covered_world_ids": [0],
            "complete_for_populated_worlds": True,
        },
        "cycle": 10,
        "game": {"paused": True},
        "bridge": {"stale": False, "snapshot_age_seconds": 0},
        "duplicants": {
            "total": 1,
            "by_instance_id": {
                "id_123": {
                    "instance_id": 123,
                    "name": "Ada",
                    "world_id": 0,
                    "chore_priorities": {"Dig": priority},
                }
            },
        },
        "resources": {"food_kcal": 100000},
        "alerts": [],
        "research": {"current": "FarmingTech", "complete": []},
        "buildings": {},
        "quality": {
            "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
            "cycle": {"status": "observed", "source": "fixture"},
            "duplicants.total": {"status": "observed", "source": "fixture"},
            path: contract_quality_fixture(DUPLICANT_CHORE_PRIORITY_CONTRACT_ID),
        },
        "sample": {"interval_seconds": 5.0, "duration_ms": 1.0},
        "writer": {
            "queue_depth": 0,
            "dropped_snapshot_count": 0,
            "fault_count": 0,
            "non_finite_numeric_count": 0,
        },
    }


def write_live_state(
    root: pathlib.Path,
    value: Dict[str, Any],
    *,
    health_status: str = "ok",
    suggestions_enabled: bool = True,
    proposal_blockers: Optional[List[str]] = None,
) -> None:
    value = copy.deepcopy(value)
    value["ts"] = iso_now()
    state_hash = canonical_state_hash(value)
    identity = {
        "session_id": value["session_id"],
        "snapshot_id": value["snapshot_id"],
        "state_hash": state_hash,
    }
    policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "suggestions_enabled": suggestions_enabled,
        "allowed_actions": ["set_duplicant_chore_priority", "set_paused"],
        "allowed_save_hashes": [value["save_id_hash"]],
        "command_ttl_seconds": 30,
        "max_pending_commands": 1,
    }
    write_json_atomic(root / "action-policy.json", policy)
    values = healthy_inputs(actions_enabled=True, suggestions_enabled=suggestions_enabled)
    values["state"] = copy.deepcopy(value)
    health = values["health"]
    health.update(
        {
            "status": health_status,
            "current_session_id": value["session_id"],
            "latest_snapshot_id": value["snapshot_id"],
            "latest_seq": value["seq"],
            "latest_state_hash": state_hash,
            "source": copy.deepcopy(identity),
            "snapshot_age_seconds": 0,
            "trust_ready": health_status == "ok",
            "trust_blockers": [] if health_status == "ok" else ["snapshot_stale"],
            "replay_mode": False,
            "session_start_observed": True,
        }
    )
    if proposal_blockers is not None:
        health["command_pipeline"] = command_pipeline(
            proposal_blockers=proposal_blockers,
        )
    for name in ("advice", "incidents", "plan", "strategy"):
        values[name]["source"] = copy.deepcopy(identity)
    values["advice"]["session_id"] = value["session_id"]
    values["incidents"]["session_id"] = value["session_id"]
    for item in values["advice"].get("items") or []:
        item["source_snapshot_id"] = value["snapshot_id"]
        item["state_hash"] = state_hash
    for item in values["incidents"].get("items") or []:
        item["snapshot_id"] = value["snapshot_id"]
        item["state_hash"] = state_hash
    for item in values["plan"].get("plan") or []:
        item["source_snapshot_id"] = value["snapshot_id"]
        item["state_hash"] = state_hash
    values["strategy"].update(
        {
            "session_id": value["session_id"],
            "snapshot_id": value["snapshot_id"],
            "snapshot_seq": value["snapshot_seq"],
            "seq": value["seq"],
            "ts": value["ts"],
            "state_hash": state_hash,
        }
    )
    frame_policy = copy.deepcopy(policy)
    frame_policy["source"] = copy.deepcopy(identity)
    values["policy"] = frame_policy
    frame = build_decision_frame(
        values["state"],
        values["health"],
        values["advice"],
        values["incidents"],
        values["plan"],
        values["strategy"],
        values["policy"],
    )
    write_json_atomic(root / "latest_state.json", value)
    write_json_atomic(root / "health.json", health)
    write_json_atomic(root / "decision-frame.json", frame)
    persist_decision_frame_archive(root, frame)


def priority_definition(before: Dict[str, Any], targets: List[int]) -> Dict[str, Any]:
    working = copy.deepcopy(before)
    steps: List[Dict[str, Any]] = []
    previous_id: str | None = None
    for index, target in enumerate(targets, start=1):
        hint = build_duplicant_priority_hint(working, instance_id=123, chore_group="Dig", priority=target)
        step_id = f"priority_{index}"
        steps.append(
            {
                "id": step_id,
                "action": hint["action"],
                "args": hint["args"],
                "dependencies": [] if previous_id is None else [previous_id],
                "preconditions": hint["preconditions"],
                "expected_effects": hint["expected_effects"],
                "reason": f"Fixture step {index}",
            }
        )
        working["duplicants"]["by_instance_id"]["id_123"]["chore_priorities"]["Dig"] = target
        previous_id = step_id
    return {
        "schema": PLAN_DEFINITION_SCHEMA,
        "title": "Priority fixture plan",
        "goal": "Verify approval-gated multi-step plan control.",
        "steps": steps,
    }


def only_command(root: pathlib.Path, directory: str) -> tuple[pathlib.Path, Dict[str, Any]]:
    paths = list(ensure_command_dirs(root)[directory].glob("*.json"))
    require(len(paths) == 1, f"expected one {directory} command, found {len(paths)}")
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8-sig"))


def complete_pending(root: pathlib.Path, after: Dict[str, Any]) -> str:
    """Drive one leaf through approval, executor ACKs, and Bridge confirmation."""

    source_path, pending = only_command(root, "pending")
    source = json.loads((root / "latest_state.json").read_text(encoding="utf-8-sig"))
    frame, _archive_path, _archive_hash = load_archived_decision_frame(
        root,
        pending["source_frame_id"],
    )
    policy = json.loads((root / "action-policy.json").read_text(encoding="utf-8-sig"))
    require(source["snapshot_id"] == pending["source_snapshot_id"], "fixture source snapshot drifted")
    require(canonical_state_hash(source) == pending["state_hash"], "fixture source hash drifted")

    approved_at = iso_now()
    command = build_approved_command(
        pending,
        policy,
        frame,
        approved_by="local_human",
        approved_at=approved_at,
    )
    source_seq = int(source["seq"])
    accepted_seq = source_seq + 1
    executed_seq = source_seq + 2
    local_effect_seq = source_seq + 3
    attempt_id = "attempt-" + "1" * 32
    direct_before = copy.deepcopy(source)
    direct_before["capture_source"] = "executor_direct_read"
    direct_after = copy.deepcopy(after)
    direct_after["capture_source"] = "executor_direct_read"
    command.update(
        {
            "status": "executed_pending_snapshot",
            "attempt_id": attempt_id,
            "accepted_ack_seq": accepted_seq,
            "executed_ack_seq": executed_seq,
            "local_effect_ack_seq": local_effect_seq,
            "completed_at": iso_now(),
            "result": {
                "before_state": direct_before,
                "after_state": direct_after,
            },
        }
    )
    confirming = ensure_command_dirs(root)["confirming"] / source_path.name
    write_json_atomic(confirming, command)
    source_path.unlink()

    archive = root / "sessions" / str(source["session_id"]) / "state.jsonl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = [source]
    for status, seq in (
        ("accepted", accepted_seq),
        ("executed", executed_seq),
        ("local_effect_observed", local_effect_seq),
    ):
        records.append(
            {
                "schema": "oni.event.v2",
                "event": "command_ack",
                "ts": iso_now(),
                "session_id": source["session_id"],
                "seq": seq,
                "id": command["id"],
                "action": command["action"],
                "attempt_id": attempt_id,
                "status": status,
            }
        )
    archive.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )

    witness = copy.deepcopy(after)
    witness["seq"] = local_effect_seq + 1
    witness["snapshot_seq"] = int(source.get("snapshot_seq") or 0) + 1
    witness["snapshot_id"] = f"{source['session_id']}:{witness['seq']}"
    write_live_state(root, witness)
    witness = json.loads((root / "latest_state.json").read_text(encoding="utf-8-sig"))
    health = json.loads((root / "health.json").read_text(encoding="utf-8-sig"))
    outcomes = build_action_outcomes(root, witness, health)
    matches = [item for item in outcomes["outcomes"] if item.get("command_id") == command["id"]]
    require(len(matches) == 1, "fixture did not build one action outcome")
    require(matches[0]["confirmation_status"] == "snapshot_confirmed", str(matches[0]["blockers"]))
    write_json_atomic(root / "action-outcomes.json", outcomes)
    committed = commit_confirmed_outcomes(root, outcomes)
    require(committed == [command["id"]], "fixture did not commit the confirmed command")
    return str(command["id"])


def move_pending(root: pathlib.Path, destination: str, *, status: str) -> str:
    source_path, command = only_command(root, "pending")
    command["status"] = status
    target = ensure_command_dirs(root)[destination] / source_path.name
    write_json_atomic(target, command)
    source_path.unlink()
    return str(command["id"])


def case_lifecycle(root: pathlib.Path) -> Dict[str, Any]:
    initial = state(priority=2)
    write_live_state(root, initial)
    plan = create_colony_plan(root, priority_definition(initial, [4, 2]))
    require(plan["status"] == "draft" and plan["revision"] == 1, "create did not produce draft revision 1")
    plan = advance_colony_plan(root)
    require(plan["status"] == "active" and plan["revision"] == 2, "advance did not activate the plan")
    require(plan["open_command_id"] and plan["steps"][0]["status"] == "awaiting_approval", "open step is wrong")
    _, pending = only_command(root, "pending")
    require(pending["requires_approval"] is True, "planned leaf command bypassed approval")
    policy = json.loads((root / "action-policy.json").read_text(encoding="utf-8-sig"))
    require(
        pending["source_frame_id"]
        == json.loads((root / "decision-frame.json").read_text(encoding="utf-8-sig"))["frame_id"],
        "planned leaf did not bind the exact current frame",
    )
    require(
        pending.get("source", {}).get("policy_hash") == canonical_hash(policy),
        "planned leaf did not bind the canonical current policy",
    )
    require(not list(ensure_command_dirs(root)["approved"].glob("*.json")), "advance created an approved command")
    require(advance_colony_plan(root)["revision"] == 2, "second advance created a duplicate open command")

    plan = pause_colony_plan(root)
    require(plan["status"] == "paused" and plan["open_command_id"], "pause lost the open command")
    plan = resume_colony_plan(root)
    require(plan["status"] == "active" and plan["open_command_id"], "resume did not revalidate the open plan")
    plan = recover_colony_plan(root)
    require(plan["status"] == "active" and plan["open_command_id"], "recover changed a unique pending leaf")

    after_first = state(priority=4)
    first_command = complete_pending(root, after_first)
    plan = advance_colony_plan(root)
    require(plan["steps"][0]["status"] == "completed", "verified leaf did not advance the checkpoint")
    require(
        plan["last_checkpoint"]["snapshot_id"] == f"{initial['session_id']}:5",
        "checkpoint did not bind the post-ACK witness",
    )
    require(
        isinstance(plan["last_checkpoint"].get("attestation_hash"), str),
        "checkpoint omitted the Bridge attestation hash",
    )
    require(plan["steps"][1]["status"] == "awaiting_approval", "dependency-ready leaf was not emitted")
    require(plan["open_command_id"] != first_command, "next step reused the completed command id")
    second_command = str(plan["open_command_id"])
    plan = abort_colony_plan(root)
    require(plan["status"] == "aborted" and plan["open_command_id"] is None, "abort did not terminate the plan")
    _, quarantined = only_command(root, "quarantine")
    require(quarantined["id"] == second_command, "abort quarantined the wrong leaf command")
    require(quarantined["quarantine_reason"] == "colony_plan_aborted", "abort quarantine reason is wrong")
    revisions = sorted((root / "colony-plans" / plan["plan_id"] / "revisions").glob("*.json"))
    require(len(revisions) == plan["revision"], "immutable revision count is not contiguous")
    require(not list(root.rglob("*.tmp")), "plan lifecycle left atomic-write temporary files")
    return {
        "plan_id": plan["plan_id"],
        "final_revision": plan["revision"],
        "first_command": first_command,
        "quarantined_command": second_command,
    }


def case_forbidden_and_approved_abort(root: pathlib.Path) -> Dict[str, Any]:
    initial = state(priority=2)
    write_live_state(root, initial)
    definition = priority_definition(initial, [4])
    unsafe = copy.deepcopy(definition)
    unsafe["steps"][0]["action"] = "set_paused"
    try:
        create_colony_plan(root, unsafe)
    except ColonyPlanError:
        forbidden_rejected = True
    else:
        forbidden_rejected = False
    require(forbidden_rejected, "non-catalogued leaf action entered a colony plan")

    bool_root = root / "strict-bool"
    write_live_state(bool_root, initial)
    bool_spoof = priority_definition(initial, [4])
    bool_spoof["steps"][0]["preconditions"][0]["value"] = 1
    try:
        create_colony_plan(bool_root, bool_spoof)
    except ColonyPlanError:
        bool_spoof_rejected = True
    else:
        bool_spoof_rejected = False
    require(bool_spoof_rejected, "boolean state accepted numeric equality in a plan precondition")

    unknown_root = root / "unknown-contract"
    write_live_state(unknown_root, initial)
    unknown_contract = priority_definition(initial, [4])
    unknown_contract["steps"][0]["expected_effects"][0]["quality_contract_id"] = "oni.qc.unknown.v1"
    try:
        create_colony_plan(unknown_root, unknown_contract)
    except ColonyPlanError:
        unknown_contract_rejected = True
    else:
        unknown_contract_rejected = False
    require(unknown_contract_rejected, "unknown dynamic quality contract entered a colony plan")

    metadata_root = root / "tampered-metadata"
    tampered_state = copy.deepcopy(initial)
    priority_path = "duplicants.by_instance_id.id_123.chore_priorities.Dig"
    tampered_state["quality"][priority_path]["scope"] = "active_world"
    write_live_state(metadata_root, tampered_state)
    metadata_plan = create_colony_plan(metadata_root, priority_definition(initial, [4]))
    try:
        advance_colony_plan(metadata_root, str(metadata_plan["plan_id"]))
    except ColonyPlanError:
        tampered_metadata_rejected = True
    else:
        tampered_metadata_rejected = False
    require(tampered_metadata_rejected, "tampered dynamic metadata authorized a colony-plan leaf")

    plan = create_colony_plan(root, definition)
    plan = advance_colony_plan(root)
    command_id = move_pending(root, "approved", status="approved")
    try:
        abort_colony_plan(root)
    except ColonyPlanError:
        abort_rejected = True
    else:
        abort_rejected = False
    require(abort_rejected, "abort rewrote or ignored an approved leaf command")
    current = load_active_plan(root)
    require(current["status"] == "active", "failed abort mutated the plan status")
    require(current["open_command_id"] == command_id, "failed abort lost the approved command binding")
    require(len(list(ensure_command_dirs(root)["approved"].glob("*.json"))) == 1, "approved command was moved")

    race_root = root / "claim-race"
    write_live_state(race_root, initial)
    race_plan = create_colony_plan(race_root, definition)
    race_plan = advance_colony_plan(race_root)
    stale_path, stale_command = only_command(race_root, "pending")
    move_pending(race_root, "approved", status="approved")
    try:
        _quarantine_pending_command(race_root, stale_path, stale_command, str(race_plan["plan_id"]))
    except ColonyPlanError:
        race_rejected = True
    else:
        race_rejected = False
    require(race_rejected, "abort claim accepted a pending file already moved to approved")
    require(len(list(ensure_command_dirs(race_root)["approved"].glob("*.json"))) == 1, "claim race moved approved data")
    return {
        "plan_id": plan["plan_id"],
        "unknown_contract_rejected": unknown_contract_rejected,
        "tampered_metadata_rejected": tampered_metadata_rejected,
        "approved_command_preserved": command_id,
        "approval_race_rejected": race_rejected,
        "boolean_type_spoof_rejected": bool_spoof_rejected,
    }


def case_revision_recovery(root: pathlib.Path) -> Dict[str, Any]:
    initial = state(priority=2)
    write_live_state(root, initial)
    plan = create_colony_plan(root, priority_definition(initial, [4]))
    directory = root / "colony-plans" / plan["plan_id"]
    (directory / "current.json").write_text("{broken", encoding="utf-8")
    recovered = recover_colony_plan(root, plan["plan_id"])
    require(recovered["revision"] == 2 and recovered["status"] == "draft", "current recovery did not use revision 1")

    revision_two = directory / "revisions" / "000002.json"
    revision_two.write_text("{broken", encoding="utf-8")
    recovered_again = recover_colony_plan(root, plan["plan_id"])
    require(recovered_again["revision"] == 2, "corrupt tail was not replaced from the valid chain")
    corrupt = list((directory / "corrupt").glob("000002-*.invalid.json"))
    require(len(corrupt) == 1, "corrupt immutable revision was not archived")

    guard_root = root / "active-guard"
    write_live_state(guard_root, initial)
    archived_plan = create_colony_plan(guard_root, priority_definition(initial, [4]))
    abort_colony_plan(guard_root, archived_plan["plan_id"])
    active_plan = create_colony_plan(guard_root, priority_definition(initial, [4]))
    try:
        recover_colony_plan(guard_root, archived_plan["plan_id"])
    except ColonyPlanError:
        active_guarded = True
    else:
        active_guarded = False
    require(active_guarded, "recover replaced a different non-terminal active plan")
    require(load_active_plan(guard_root)["plan_id"] == active_plan["plan_id"], "active plan pointer was replaced")
    return {
        "plan_id": plan["plan_id"],
        "recovered_revision": recovered_again["revision"],
        "archived_corrupt_revision": corrupt[0].name,
        "different_active_plan_guarded": active_guarded,
    }


def case_stale_and_failed(root: pathlib.Path) -> Dict[str, Any]:
    initial = state(priority=2)
    write_live_state(root, initial)
    create_colony_plan(root, priority_definition(initial, [4]))
    advance_colony_plan(root)
    pause_colony_plan(root)
    write_live_state(root, initial, health_status="stale")
    paused = resume_colony_plan(root)
    require(paused["status"] == "paused", "stale resume activated the plan")
    require(paused["pause_reason"] == "resume_revalidation_failed", "stale resume reason is wrong")

    write_live_state(root, initial)
    active = resume_colony_plan(root)
    require(active["status"] == "active", "fresh resume did not activate the plan")
    failed_id = move_pending(root, "failed", status="failed")
    failed = recover_colony_plan(root)
    require(failed["status"] == "failed", "failed leaf did not terminate the plan as failed")
    require(failed["steps"][0]["status"] == "failed", "failed step status is wrong")
    return {"failed_command": failed_id, "final_status": failed["status"]}


def case_forged_done_is_not_checkpoint(root: pathlib.Path) -> Dict[str, Any]:
    initial = state(priority=2)
    write_live_state(root, initial)
    create_colony_plan(root, priority_definition(initial, [4]))
    plan = advance_colony_plan(root)
    initial_checkpoint = copy.deepcopy(plan["last_checkpoint"])
    source_path, command = only_command(root, "pending")
    command["status"] = "confirmed"
    command["completed_at"] = iso_now()
    command["result"] = {
        "after_state": state(priority=4, seq=5),
        "confirmed_snapshot_id": f"{initial['session_id']}:5",
        "confirmed_seq": 5,
        "confirmed_state_hash": "sha256:" + "f" * 64,
    }
    write_json_atomic(ensure_command_dirs(root)["done"] / source_path.name, command)
    source_path.unlink()

    reconciled = advance_colony_plan(root)
    require(reconciled["status"] == "active", "forged done terminated the plan")
    require(
        reconciled["steps"][0]["status"] == "awaiting_confirmation",
        "forged executor result completed a plan checkpoint",
    )
    require(
        reconciled.get("last_checkpoint") == initial_checkpoint,
        "forged done replaced the source checkpoint",
    )
    return {
        "plan_id": plan["plan_id"],
        "command_id": command["id"],
        "step_status": reconciled["steps"][0]["status"],
        "checkpoint_created": False,
    }


def run_case(name: str, callback: Callable[[pathlib.Path], Dict[str, Any]], root: pathlib.Path) -> Dict[str, Any]:
    case_root = root / name
    case_root.mkdir(parents=True)
    try:
        details = callback(case_root)
        return {"name": name, "status": "passed", "details": details}
    except Exception as exc:
        return {"name": name, "status": "failed", "classification": "behavior_fail", "error": str(exc)}


def run_static(report_path: pathlib.Path) -> int:
    with tempfile.TemporaryDirectory(prefix="oni-colony-plan-") as temporary:
        root = pathlib.Path(temporary)
        cases = [
            run_case("lifecycle", case_lifecycle, root),
            run_case("forbidden_approved_abort", case_forbidden_and_approved_abort, root),
            run_case("revision_recovery", case_revision_recovery, root),
            run_case("stale_failed", case_stale_and_failed, root),
            run_case("forged_done_is_not_checkpoint", case_forged_done_is_not_checkpoint, root),
        ]
    passed = all(item["status"] == "passed" for item in cases)
    report = {
        "schema": "oni.colony-plan-verification.v1",
        "generated_at": iso_now(),
        "mode": "static",
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "cases": cases,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in cases:
        print(f"[{item['status'].upper()}] {item['name']}: {json.dumps(item.get('details') or item.get('error'), ensure_ascii=False, sort_keys=True)}")
    print(f"Report: {report_path}")
    return 0 if passed else 1


def live_definition(latest: Dict[str, Any]) -> Dict[str, Any]:
    by_instance = ((latest.get("duplicants") or {}).get("by_instance_id") or {})
    for target in by_instance.values():
        if not isinstance(target, dict):
            continue
        instance_id = target.get("instance_id")
        priorities = target.get("chore_priorities") if isinstance(target.get("chore_priorities"), dict) else {}
        for group, current in priorities.items():
            if isinstance(instance_id, int) and isinstance(current, int) and not isinstance(current, bool) and 0 <= current <= 5:
                desired = (current + 1) % 6
                hint = build_duplicant_priority_hint(
                    latest,
                    instance_id=instance_id,
                    chore_group=str(group),
                    priority=desired,
                )
                return {
                    "schema": PLAN_DEFINITION_SCHEMA,
                    "title": "Batch D live control-plane acceptance",
                    "goal": "Create and quarantine one approval-gated leaf without executing it.",
                    "steps": [
                        {
                            "id": "approval_gate_probe",
                            "action": hint["action"],
                            "args": hint["args"],
                            "dependencies": [],
                            "preconditions": hint["preconditions"],
                            "expected_effects": hint["expected_effects"],
                            "reason": "Batch D live approval-gate verification",
                        }
                    ],
                }
    raise ColonyPlanError("No observed duplicant priority target is available for live Batch D verification.")


def run_live(bridge_dir: pathlib.Path, report_path: pathlib.Path) -> int:
    bridge_dir = bridge_dir.expanduser().resolve()
    latest = json.loads((bridge_dir / "latest_state.json").read_text(encoding="utf-8-sig"))
    definition = live_definition(latest)
    plan = create_colony_plan(bridge_dir, definition)
    plan_id = str(plan["plan_id"])
    plan = advance_colony_plan(bridge_dir, plan_id)
    command_id = str(plan["open_command_id"])
    pending_path, command = only_command(bridge_dir, "pending")
    require(command["id"] == command_id and command["requires_approval"] is True, "live leaf is not approval gated")
    require(not list(ensure_command_dirs(bridge_dir)["approved"].glob(f"*{command_id}*.json")), "live leaf was auto-approved")
    plan = pause_colony_plan(bridge_dir, plan_id)
    plan = resume_colony_plan(bridge_dir, plan_id)
    plan = recover_colony_plan(bridge_dir, plan_id)
    require(plan["status"] == "active" and plan["open_command_id"] == command_id, "live recovery lost the pending leaf")
    plan = abort_colony_plan(bridge_dir, plan_id)
    quarantine_matches = []
    for path in ensure_command_dirs(bridge_dir)["quarantine"].glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if value.get("id") == command_id:
            quarantine_matches.append((path, value))
    require(len(quarantine_matches) == 1, "live abort did not quarantine exactly one pending leaf")
    require(not pending_path.exists(), "live pending leaf remained after abort")
    report = {
        "schema": "oni.colony-plan-verification.v1",
        "generated_at": iso_now(),
        "mode": "live",
        "status": "passed",
        "classification": None,
        "session_id": latest.get("session_id"),
        "save_id_hash": latest.get("save_id_hash"),
        "plan_id": plan_id,
        "final_revision": plan["revision"],
        "final_status": plan["status"],
        "command_id": command_id,
        "command_requires_approval": command["requires_approval"],
        "command_final_directory": "quarantine",
        "game_command_executed": False,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    marker = PROJECT_ROOT / "reports" / "runtime-markers" / "colony_plan_runtime.passed"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--bridge-dir", type=pathlib.Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--report", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    default_name = "colony-plan-live.json" if args.live else "colony-plan-verification.json"
    report = (args.report or PROJECT_ROOT / "reports" / default_name).expanduser().resolve()
    try:
        return run_live(args.bridge_dir, report) if args.live else run_static(report)
    except Exception as exc:
        failure = {
            "schema": "oni.colony-plan-verification.v1",
            "generated_at": iso_now(),
            "mode": "live" if args.live else "static",
            "status": "failed",
            "classification": "behavior_fail",
            "error": str(exc),
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
