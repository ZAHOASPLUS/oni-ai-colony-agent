#!/usr/bin/env python3
"""Adversarial offline fixtures for the G0 priority/inverse lineage finalizer."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import shutil
import sys
import tempfile
from typing import Any, Dict, Mapping, Sequence
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_outcomes import (
    ActionOutcomeError,
    build_action_outcomes,
    confirmed_command_approval_bundle_path,
    load_confirmed_command_approval_bundle,
    recover_staged_confirmed_outcomes,
    stage_confirmed_outcomes_commit,
)
from agent.advanced_actions import build_duplicant_priority_hint
from agent.approval_receipt import canonical_hash, issue_approval_receipt
from agent.command_queue import COMMAND_PIPELINE_SCHEMA, command_pipeline_readiness
from agent.decision import build_decision_frame
from agent.decision_frame_archive import (
    decision_frame_archive_path,
    persist_decision_frame_archive,
    prune_decision_frame_archives,
)
from agent.field_contracts import (
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.state_quality import canonical_state_hash, snapshot_freshness
from bridge import oni_bridge
from scripts import finalize_priority_pair_lineage as pair_workflow
from scripts.finalize_priority_pair_lineage import (
    PriorityPairError,
    freeze_confirmed_command,
    validate_priority_inverse,
    verify_priority_pair,
    write_report_create_only,
)
from scripts.manage_save_lineage import (
    LineageError,
    activate,
    checkpoint,
    load_json,
    path_id,
    rehash_checkpoints,
    reserve,
    sha256_file,
    write_json_atomic,
)
from scripts.verify_decision_frame import healthy_inputs


MOD_VERSION = "0.7.0"
ACTION = "set_duplicant_chore_priority"
INSTANCE_ID = 123
EXPECTED_NAME = "Ada"
CHORE_GROUP = "Dig"
VALIDATION_RUN_ID = "priority-pair-startup-fixture"
VALIDATION_PROCESS_ID = 424242
STARTUP_BINDING_HASH = "sha256:" + "1" * 64
STARTUP_INTENT_HASH = "sha256:" + "2" * 64
RUNTIME_CONFIG_EVIDENCE_SHA256 = "sha256:" + "3" * 64
ASSERTION_COUNT = 0


def require(condition: bool, message: str) -> None:
    global ASSERTION_COUNT
    ASSERTION_COUNT += 1
    if not condition:
        raise AssertionError(message)


def iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def startup_expectations(loader_path_id: str, loader_save_hash: str) -> Dict[str, Any]:
    return {
        "validation_run_id": VALIDATION_RUN_ID,
        "validation_process_id": VALIDATION_PROCESS_ID,
        "startup_binding_hash": STARTUP_BINDING_HASH,
        "startup_intent_hash": STARTUP_INTENT_HASH,
        "runtime_config_evidence_sha256": RUNTIME_CONFIG_EVIDENCE_SHA256,
        "loader_path_id_upper": loader_path_id,
        "loader_save_sha256": loader_save_hash,
        "game_build": "U59-priority-pair-fixture",
        "mod_version": MOD_VERSION,
    }


def bind_startup_record(
    record: Mapping[str, Any],
    expectations: Mapping[str, Any],
    *,
    active_path_id: str,
    save_hash: str,
    requested_path_id: str | None = None,
) -> Dict[str, Any]:
    result = copy.deepcopy(dict(record))
    result.update(
        {
            "validation_run_id": expectations["validation_run_id"],
            "validation_process_id": expectations["validation_process_id"],
            "startup_binding_hash": expectations["startup_binding_hash"],
            "startup_intent_hash": expectations["startup_intent_hash"],
            "runtime_config_evidence_sha256": expectations[
                "runtime_config_evidence_sha256"
            ],
            "startup_binding_status": "verified",
            "requested_save_path_id": (
                expectations["loader_path_id_upper"]
                if requested_path_id is None
                else requested_path_id
            ),
            "active_save_path_id": active_path_id,
            "save_id_hash": save_hash,
            "game_build": expectations["game_build"],
            "mod_version": expectations["mod_version"],
        }
    )
    return result


def protocol_event(
    event: str,
    session_id: str,
    seq: int,
    timestamp: dt.datetime,
    expectations: Mapping[str, Any],
    *,
    active_path_id: str,
    save_hash: str,
    **fields: Any,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema": "oni.event.v2",
        "event": event,
        "ts": iso(timestamp),
        "session_id": session_id,
        "seq": seq,
        "save_dirty": False,
        "world_id": 0,
    }
    record.update(fields)
    return bind_startup_record(
        record,
        expectations,
        active_path_id=active_path_id,
        save_hash=save_hash,
    )


def save_protocol_records(
    session_id: str,
    start_seq: int,
    timestamp: dt.datetime,
    expectations: Mapping[str, Any],
    *,
    active_path_before: str,
    target_path: str,
    before_hash: str,
    after_hash: str,
    control_id: str,
    completed: Mapping[str, Any],
) -> list[Dict[str, Any]]:
    control = protocol_event(
        "validation_control_started",
        session_id,
        start_seq,
        timestamp,
        expectations,
        active_path_id=active_path_before,
        save_hash=before_hash,
        operation="save_checkpoint",
        test_run_id=VALIDATION_RUN_ID,
        control_id=control_id,
        target_save_path_id=target_path,
    )
    started = protocol_event(
        "save_write_started",
        session_id,
        start_seq + 1,
        timestamp + dt.timedelta(milliseconds=100),
        expectations,
        active_path_id=active_path_before,
        save_hash=before_hash,
        is_auto_save=False,
        update_save_pointer=True,
        before_save_id_hash=before_hash,
    )
    started["requested_save_path_id"] = target_path
    completion = bind_startup_record(
        completed,
        expectations,
        active_path_id=target_path,
        save_hash=after_hash,
        requested_path_id=target_path,
    )
    return [control, started, completion]


def reload_protocol_records(
    session_id: str,
    start_seq: int,
    timestamp: dt.datetime,
    expectations: Mapping[str, Any],
    *,
    active_path_id: str,
    save_hash: str,
    control_id: str,
) -> list[Dict[str, Any]]:
    control = protocol_event(
        "validation_control_started",
        session_id,
        start_seq,
        timestamp,
        expectations,
        active_path_id=active_path_id,
        save_hash=save_hash,
        operation="reload_checkpoint",
        test_run_id=VALIDATION_RUN_ID,
        control_id=control_id,
        expected_save_path_id=active_path_id,
        expected_save_id_hash=save_hash,
    )
    unloaded = protocol_event(
        "save_unloaded",
        session_id,
        start_seq + 1,
        timestamp + dt.timedelta(milliseconds=100),
        expectations,
        active_path_id=active_path_id,
        save_hash=save_hash,
        reason="save_loader_load_start",
    )
    ended = protocol_event(
        "session_ended",
        session_id,
        start_seq + 2,
        timestamp + dt.timedelta(milliseconds=200),
        expectations,
        active_path_id=active_path_id,
        save_hash=save_hash,
        reason="save_loader_load_start",
    )
    return [control, unloaded, ended]


def append_records(path: pathlib.Path, *records: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            raw_record = (
                pair_workflow.snapshot_core_without_bridge(record)
                if record.get("event") == "state_snapshot"
                else dict(record)
            )
            handle.write(
                json.dumps(raw_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                + "\n"
            )


def priority_snapshot(
    session_id: str,
    seq: int,
    save_hash: str,
    priority: int,
    timestamp: dt.datetime,
    *,
    snapshot_seq: int = 1,
) -> Dict[str, Any]:
    priority_path = f"duplicants.by_instance_id.id_{INSTANCE_ID}.chore_priorities.{CHORE_GROUP}"
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": iso(timestamp),
        "session_id": session_id,
        "seq": seq,
        "snapshot_seq": snapshot_seq,
        "snapshot_id": f"{session_id}:{seq}",
        "game_build": "U59-priority-pair-fixture",
        "mod_version": MOD_VERSION,
        "save_id_hash": save_hash,
        "save_dirty": False,
        "world_id": 0,
        # manage_save_lineage refuses a snapshot without an enforced
        # autosave guard (review P0-1).
        "autosave": {
            "status": "enforced",
            "guard_active": True,
            "cycle_interval": 0,
            "suppression_patch_installed": True,
        },
        "observation_scope": {
            "active_world_id": 0,
            "populated_world_ids": [0],
            "covered_world_ids": [0],
            "complete_for_populated_worlds": True,
        },
        "game": {"paused": True},
        "duplicants": {
            "by_instance_id": {
                f"id_{INSTANCE_ID}": {
                    "instance_id": INSTANCE_ID,
                    "name": EXPECTED_NAME,
                    "world_id": 0,
                    "chore_priorities": {CHORE_GROUP: priority, "Build": 3},
                }
            }
        },
        "quality": {
            "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
            priority_path: contract_quality_fixture(DUPLICANT_CHORE_PRIORITY_CONTRACT_ID),
        },
        "bridge": {"stale": False, "trust_ready": True, "trust_blockers": []},
        "sample": {"interval_seconds": 5.0, "duration_ms": 0.25},
        "writer": {
            "queue_depth": 0,
            "dropped_snapshot_count": 0,
            "fault_count": 0,
            "non_finite_numeric_count": 0,
        },
    }


def save_event(
    session_id: str,
    seq: int,
    before_hash: str,
    after_hash: str,
    save_path_id: str,
    timestamp: dt.datetime,
) -> Dict[str, Any]:
    return {
        "schema": "oni.event.v2",
        "event": "save_write_completed",
        "ts": iso(timestamp),
        "session_id": session_id,
        "seq": seq,
        "game_build": "U59-priority-pair-fixture",
        "mod_version": MOD_VERSION,
        "save_id_hash": after_hash,
        "save_dirty": False,
        "world_id": 0,
        "success": True,
        "is_auto_save": False,
        "update_save_pointer": True,
        "file_rewrite_observed": True,
        "save_file_readable": True,
        "requested_save_path_id": save_path_id,
        "result_save_path_id": save_path_id,
        "active_save_path_id": save_path_id,
        "before_save_id_hash": before_hash,
        "written_save_id_hash": after_hash,
        "active_save_id_hash": after_hash,
    }


def enabled_policy(save_hash: str, timestamp: dt.datetime) -> Dict[str, Any]:
    return {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "suggestions_enabled": True,
        "allowed_actions": [ACTION],
        "allowed_save_hashes": [save_hash],
        "command_ttl_seconds": 60,
        "max_pending_commands": 20,
        "generated_at": iso(timestamp),
    }


def disabled_policy(timestamp: dt.datetime) -> Dict[str, Any]:
    return {
        "schema": "oni.action-policy.v1",
        "actions_enabled": False,
        "suggestions_enabled": False,
        "allowed_actions": [],
        "allowed_save_hashes": [],
        "command_ttl_seconds": 60,
        "max_pending_commands": 20,
        "generated_at": iso(timestamp),
    }


def authorizing_frame(
    bridge: pathlib.Path,
    state: Dict[str, Any],
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    values["state"] = copy.deepcopy(state)
    state_hash = canonical_state_hash(state)
    identity = {
        "session_id": state["session_id"],
        "snapshot_id": state["snapshot_id"],
        "state_hash": state_hash,
    }
    values["health"].update(
        {
            "status": "ok",
            "current_session_id": state["session_id"],
            "latest_snapshot_id": state["snapshot_id"],
            "latest_state_hash": state_hash,
            "source": copy.deepcopy(identity),
            "trust_ready": True,
            "replay_mode": False,
            "session_start_observed": True,
        }
    )
    for name in ("advice", "incidents", "plan", "strategy"):
        values[name]["source"] = copy.deepcopy(identity)
    values["advice"]["session_id"] = state["session_id"]
    values["incidents"]["session_id"] = state["session_id"]
    for item in values["advice"].get("items") or []:
        item["source_snapshot_id"] = state["snapshot_id"]
        item["state_hash"] = state_hash
    for item in values["incidents"].get("items") or []:
        item["snapshot_id"] = state["snapshot_id"]
        item["state_hash"] = state_hash
    for item in values["plan"].get("plan") or []:
        item["source_snapshot_id"] = state["snapshot_id"]
        item["state_hash"] = state_hash
    values["strategy"].update(
        {
            "session_id": state["session_id"],
            "snapshot_id": state["snapshot_id"],
            "snapshot_seq": state["snapshot_seq"],
            "seq": state["seq"],
            "ts": state["ts"],
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
    require(frame["trust"]["status"] == "trusted", "fixture frame is not trusted")
    require(frame["readiness"]["propose"] == {"ready": True, "blockers": []}, "fixture cannot propose")
    write_json_atomic(bridge / "decision-frame.json", frame)
    persist_decision_frame_archive(bridge, frame)
    return frame


def direct_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(dict(state))
    result["capture_source"] = "executor_direct_read"
    return result


def command_ack(
    source: Mapping[str, Any],
    command: Mapping[str, Any],
    status: str,
    timestamp: dt.datetime,
) -> Dict[str, Any]:
    sequence_fields = {
        "accepted": "accepted_ack_seq",
        "executed": "executed_ack_seq",
        "local_effect_observed": "local_effect_ack_seq",
    }
    record = {
        "schema": "oni.event.v2",
        "event": "command_ack",
        "ts": iso(timestamp),
        "session_id": source["session_id"],
        "seq": command[sequence_fields[status]],
        "game_build": source["game_build"],
        "mod_version": source["mod_version"],
        "save_id_hash": source["save_id_hash"],
        "save_dirty": False,
        "world_id": source["world_id"],
        "id": command["id"],
        "action": command["action"],
        "status": status,
        "stage": status,
        "message": f"fixture {status}",
        "attempt_id": command["attempt_id"],
    }
    for field in (
        "validation_run_id",
        "validation_process_id",
        "startup_binding_hash",
        "startup_intent_hash",
        "runtime_config_evidence_sha256",
        "startup_binding_status",
        "requested_save_path_id",
        "active_save_path_id",
    ):
        record[field] = source[field]
    return record


def build_priority_command(
    command_id: str,
    source: Dict[str, Any],
    witness: Dict[str, Any],
    policy: Dict[str, Any],
    frame: Dict[str, Any],
    target_priority: int,
) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
    source_at = dt.datetime.fromisoformat(source["ts"])
    hint = build_duplicant_priority_hint(
        source,
        instance_id=INSTANCE_ID,
        chore_group=CHORE_GROUP,
        priority=target_priority,
    )
    command: Dict[str, Any] = {
        "schema": "oni.command.v3",
        "id": command_id,
        "mode": "runtime_validation",
        "status": "approved",
        "action": ACTION,
        "args": copy.deepcopy(hint["args"]),
        "session_id": source["session_id"],
        "save_id_hash": source["save_id_hash"],
        "world_id": source["world_id"],
        "source_incident_id": None,
        "source_snapshot_id": source["snapshot_id"],
        "state_hash": canonical_state_hash(source),
        "source_frame_id": frame["frame_id"],
        "created_at": iso(source_at + dt.timedelta(milliseconds=100)),
        "expires_at": iso(source_at + dt.timedelta(seconds=60)),
        "risk": "medium",
        "reason": "Offline G0 priority pair fixture",
        "evidence": {"source_snapshot_id": source["snapshot_id"]},
        "preconditions": copy.deepcopy(hint["preconditions"]),
        "expected_effects": copy.deepcopy(hint["expected_effects"]),
        "requires_approval": True,
        "source": {
            "kind": "runtime_validation",
            "state_seq": source["seq"],
            "state_ts": source["ts"],
            "policy_hash": canonical_hash(policy),
        },
        "approved_by": "local_human",
        "approved_at": iso(source_at + dt.timedelta(milliseconds=200)),
    }
    command["approval_receipt"] = issue_approval_receipt(
        command,
        policy,
        frame,
        approved_by="local_human",
        approved_at=command["approved_at"],
    )
    command.update(
        {
            "status": "executed_pending_snapshot",
            "attempt_id": "attempt-" + command_id.encode("utf-8").hex()[:32].ljust(32, "0"),
            "accepted_ack_seq": source["seq"] + 1,
            "executed_ack_seq": source["seq"] + 2,
            "local_effect_ack_seq": source["seq"] + 3,
            "completed_at": iso(source_at + dt.timedelta(milliseconds=900)),
            "result": {
                "before_state": direct_state(source),
                "after_state": direct_state(witness),
            },
        }
    )
    records = [
        command_ack(source, command, "accepted", source_at + dt.timedelta(milliseconds=400)),
        command_ack(source, command, "executed", source_at + dt.timedelta(milliseconds=600)),
        command_ack(source, command, "local_effect_observed", source_at + dt.timedelta(milliseconds=800)),
    ]
    return command, records


def health_for(witness: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "trust_ready": True,
        "trust_blockers": [],
        "current_session_id": witness["session_id"],
        "latest_snapshot_id": witness["snapshot_id"],
        "latest_seq": witness["seq"],
        "source": {
            "session_id": witness["session_id"],
            "snapshot_id": witness["snapshot_id"],
            "state_hash": canonical_state_hash(dict(witness)),
        },
    }


def confirm_command(
    bridge: pathlib.Path,
    source: Dict[str, Any],
    witness: Dict[str, Any],
    command: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    frame: Dict[str, Any],
    *,
    inject_missing_frame: bool,
) -> Dict[str, Any]:
    confirming_path = bridge / "commands" / "confirming" / f"{command['id']}.json"
    write_json_atomic(confirming_path, command)
    append_records(bridge / "state.jsonl", *records, witness)
    session_log = bridge / "sessions" / source["session_id"] / "state.jsonl"
    append_records(session_log, source, *records, witness)
    outcomes = build_action_outcomes(bridge, witness, health_for(witness))
    root_certificate = bridge / "action-outcomes.json"
    session_certificate = bridge / "sessions" / source["session_id"] / "action-outcomes.json"
    write_json_atomic(root_certificate, outcomes)
    write_json_atomic(session_certificate, outcomes)
    stage_confirmed_outcomes_commit(bridge, outcomes)

    frame_path = decision_frame_archive_path(bridge, frame["frame_id"])
    publication_write_blocked = False
    missing_frame_blocked = False
    conflicting_bundle_blocked = False
    if inject_missing_frame:
        with mock.patch(
            "agent.action_outcomes._publish_json_create_only",
            side_effect=OSError("fixture injected approval-bundle publication failure"),
        ):
            try:
                recover_staged_confirmed_outcomes(bridge)
            except OSError:
                publication_write_blocked = True
        require(publication_write_blocked, "approval-bundle publication failure did not block commit")
        require(confirming_path.is_file(), "publication failure did not remain confirming")
        require(
            not confirmed_command_approval_bundle_path(bridge, command["id"]).exists(),
            "publication failure left partial approval evidence",
        )

        conflicting_bundle_path = confirmed_command_approval_bundle_path(bridge, command["id"])
        write_json_atomic(conflicting_bundle_path, {"schema": "fixture-conflict"})
        try:
            recover_staged_confirmed_outcomes(bridge)
        except ActionOutcomeError:
            conflicting_bundle_blocked = True
        require(conflicting_bundle_blocked, "conflicting create-only bundle did not block commit")
        require(confirming_path.is_file(), "conflicting bundle did not remain confirming")
        conflicting_bundle_path.unlink()

        frame_path.unlink()
        try:
            recover_staged_confirmed_outcomes(bridge)
        except ActionOutcomeError:
            missing_frame_blocked = True
        require(missing_frame_blocked, "missing frame did not block confirming-to-done")
        require(confirming_path.is_file(), "failed pre-done bundle did not remain confirming")
        require(
            not (bridge / "commands" / "done" / confirming_path.name).exists(),
            "failed pre-done bundle entered done",
        )
        require(
            not confirmed_command_approval_bundle_path(bridge, command["id"]).exists(),
            "failed pre-done bundle left partial evidence",
        )
        persist_decision_frame_archive(bridge, frame)

    committed = recover_staged_confirmed_outcomes(bridge)
    require(committed == [command["id"]], f"confirmed command did not commit: {committed!r}")
    done = load_json(bridge / "commands" / "done" / f"{command['id']}.json")
    bundle, bundle_path = load_confirmed_command_approval_bundle(bridge, command["id"])
    require(bundle["final_command"] == done, "pre-done bundle did not freeze final command")
    require(bundle_path.is_file(), "pre-done bundle is missing")
    # Idempotent recovery must accept the exact existing bundle.
    require(
        recover_staged_confirmed_outcomes(bridge) == [command["id"]],
        "idempotent confirmation recovery changed the bundle contract",
    )
    return {
        "done": done,
        "bundle": bundle,
        "publication_write_blocked": publication_write_blocked,
        "missing_frame_blocked": missing_frame_blocked,
        "conflicting_bundle_blocked": conflicting_bundle_blocked,
    }


def build_valid_fixture(root: pathlib.Path) -> Dict[str, Any]:
    bridge = root / "bridge"
    bridge.mkdir(parents=True)
    save_path = root / "priority-pair.sav"
    source_path = root / "source.sav"
    source_path.write_bytes(b"source-priority-pair")
    source_hash = sha256_file(source_path)
    loader_path_id = path_id(source_path)
    expectations = startup_expectations(loader_path_id, source_hash)
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    reservation_source = bind_startup_record(priority_snapshot(
        "session-reservation",
        2,
        source_hash,
        2,
        now - dt.timedelta(seconds=60),
    ), expectations, active_path_id=loader_path_id, save_hash=source_hash)
    write_json_atomic(bridge / "latest_state.json", reservation_source)
    write_json_atomic(bridge / "action-policy.json", disabled_policy(now - dt.timedelta(seconds=12)))
    append_records(bridge / "state.jsonl", reservation_source)
    reserved = reserve(
        argparse.Namespace(
            run_id="priority-pair-fixture",
            bridge_dir=bridge,
            save_path=save_path,
            mod_version=MOD_VERSION,
            source_kind="save_as_copy",
            allow_external_save=True,
        )
    )
    manifest_path = pathlib.Path(reserved["manifest"])

    save_path.write_bytes(b"priority-pair-initial")
    initial_hash = sha256_file(save_path)
    activation_save = save_event(
        reservation_source["session_id"],
        5,
        source_hash,
        initial_hash,
        path_id(save_path),
        now - dt.timedelta(seconds=11),
    )
    activation_save = bind_startup_record(
        activation_save,
        expectations,
        active_path_id=path_id(save_path),
        save_hash=initial_hash,
        requested_path_id=path_id(save_path),
    )
    forward_source = bind_startup_record(priority_snapshot(
        "session-priority-forward",
        2,
        initial_hash,
        2,
        now - dt.timedelta(seconds=10),
    ), expectations, active_path_id=path_id(save_path), save_hash=initial_hash)
    append_records(bridge / "state.jsonl", activation_save, forward_source)
    write_json_atomic(bridge / "latest_state.json", forward_source)
    activate(argparse.Namespace(manifest=manifest_path, bridge_dir=bridge))

    forward_policy = enabled_policy(initial_hash, now - dt.timedelta(seconds=10))
    write_json_atomic(bridge / "action-policy.json", forward_policy)
    forward_frame = authorizing_frame(bridge, forward_source, forward_policy)
    forward_witness = bind_startup_record(priority_snapshot(
        forward_source["session_id"],
        6,
        initial_hash,
        4,
        now - dt.timedelta(seconds=8, milliseconds=800),
        snapshot_seq=2,
    ), expectations, active_path_id=path_id(save_path), save_hash=initial_hash)
    forward_command, forward_records = build_priority_command(
        "cmd-priority-forward-fixture",
        forward_source,
        forward_witness,
        forward_policy,
        forward_frame,
        4,
    )
    forward_confirmation = confirm_command(
        bridge,
        forward_source,
        forward_witness,
        forward_command,
        forward_records,
        forward_frame,
        inject_missing_frame=True,
    )
    prune_forward = prune_decision_frame_archives(bridge, max_unreferenced=0)
    require(prune_forward["pruned_count"] >= 1, "done frame was unexpectedly retained")
    require(
        not decision_frame_archive_path(bridge, forward_frame["frame_id"]).exists(),
        "forward frame survived forced same-round prune",
    )
    forward_freeze = freeze_confirmed_command(
        manifest_path,
        bridge,
        step_id="priority_forward",
        command_id=forward_command["id"],
    )

    write_json_atomic(bridge / "action-policy.json", disabled_policy(now - dt.timedelta(seconds=8)))
    save_path.write_bytes(b"priority-pair-after-forward")
    forward_save_hash = sha256_file(save_path)
    forward_save = save_event(
        forward_source["session_id"],
        9,
        initial_hash,
        forward_save_hash,
        path_id(save_path),
        now - dt.timedelta(seconds=8),
    )
    forward_save = bind_startup_record(
        forward_save,
        expectations,
        active_path_id=path_id(save_path),
        save_hash=forward_save_hash,
        requested_path_id=path_id(save_path),
    )
    inverse_source = bind_startup_record(priority_snapshot(
        "session-priority-inverse",
        2,
        forward_save_hash,
        4,
        now - dt.timedelta(seconds=7),
    ), expectations, active_path_id=path_id(save_path), save_hash=forward_save_hash)
    append_records(bridge / "state.jsonl", forward_save, inverse_source)
    write_json_atomic(bridge / "latest_state.json", inverse_source)
    checkpoint(
        argparse.Namespace(
            manifest=manifest_path,
            bridge_dir=bridge,
            step_id="priority_forward",
            scenario="confirmed priority 2 to 4 persists after explicit save/reload",
            outcome="confirmed",
            expected_mutation="persisted_change",
            command_id=forward_command["id"],
            evidence=[forward_freeze["path"]],
        )
    )

    inverse_policy = enabled_policy(forward_save_hash, now - dt.timedelta(seconds=7))
    write_json_atomic(bridge / "action-policy.json", inverse_policy)
    inverse_frame = authorizing_frame(bridge, inverse_source, inverse_policy)
    inverse_witness = bind_startup_record(priority_snapshot(
        inverse_source["session_id"],
        6,
        forward_save_hash,
        2,
        now - dt.timedelta(seconds=5, milliseconds=800),
        snapshot_seq=2,
    ), expectations, active_path_id=path_id(save_path), save_hash=forward_save_hash)
    inverse_command, inverse_records = build_priority_command(
        "cmd-priority-inverse-fixture",
        inverse_source,
        inverse_witness,
        inverse_policy,
        inverse_frame,
        2,
    )
    inverse_confirmation = confirm_command(
        bridge,
        inverse_source,
        inverse_witness,
        inverse_command,
        inverse_records,
        inverse_frame,
        inject_missing_frame=False,
    )
    prune_inverse = prune_decision_frame_archives(bridge, max_unreferenced=0)
    require(prune_inverse["pruned_count"] >= 1, "inverse done frame was unexpectedly retained")
    inverse_freeze = freeze_confirmed_command(
        manifest_path,
        bridge,
        step_id="priority_inverse",
        command_id=inverse_command["id"],
    )

    write_json_atomic(bridge / "action-policy.json", disabled_policy(now - dt.timedelta(seconds=5)))
    save_path.write_bytes(b"priority-pair-after-inverse")
    inverse_save_hash = sha256_file(save_path)
    inverse_save = save_event(
        inverse_source["session_id"],
        9,
        forward_save_hash,
        inverse_save_hash,
        path_id(save_path),
        now - dt.timedelta(seconds=5),
    )
    inverse_save = bind_startup_record(
        inverse_save,
        expectations,
        active_path_id=path_id(save_path),
        save_hash=inverse_save_hash,
        requested_path_id=path_id(save_path),
    )
    final_reload = bind_startup_record(priority_snapshot(
        "session-priority-final",
        2,
        inverse_save_hash,
        2,
        now - dt.timedelta(seconds=4),
    ), expectations, active_path_id=path_id(save_path), save_hash=inverse_save_hash)
    final_start = protocol_event(
        "session_started",
        final_reload["session_id"],
        1,
        now - dt.timedelta(seconds=4, milliseconds=500),
        expectations,
        active_path_id=path_id(save_path),
        save_hash=inverse_save_hash,
        reason="save_loader_load_complete",
    )
    final_ended = protocol_event(
        "session_ended",
        final_reload["session_id"],
        3,
        now - dt.timedelta(seconds=3, milliseconds=800),
        expectations,
        active_path_id=path_id(save_path),
        save_hash=inverse_save_hash,
        reason="application_quit",
    )
    final_shutdown = protocol_event(
        "probe_shutdown",
        final_reload["session_id"],
        4,
        now - dt.timedelta(seconds=3, milliseconds=700),
        expectations,
        active_path_id=path_id(save_path),
        save_hash=inverse_save_hash,
    )
    final_raw = pair_workflow.snapshot_core_without_bridge(final_reload)
    final_session_records = [final_start, final_raw, final_ended, final_shutdown]
    source_path_id = path_id(source_path)
    derivative_path_id = path_id(save_path)
    origin_start = protocol_event(
        "session_started",
        reservation_source["session_id"],
        1,
        now - dt.timedelta(seconds=60, milliseconds=500),
        expectations,
        active_path_id=source_path_id,
        save_hash=source_hash,
        reason="save_loader_load_complete",
    )
    origin_save_records = save_protocol_records(
        reservation_source["session_id"],
        3,
        now - dt.timedelta(seconds=11, milliseconds=200),
        expectations,
        active_path_before=source_path_id,
        target_path=derivative_path_id,
        before_hash=source_hash,
        after_hash=initial_hash,
        control_id="save-origin-derivative",
        completed=activation_save,
    )
    origin_reload_records = reload_protocol_records(
        reservation_source["session_id"],
        6,
        now - dt.timedelta(seconds=10, milliseconds=900),
        expectations,
        active_path_id=derivative_path_id,
        save_hash=initial_hash,
        control_id="reload-origin-derivative",
    )
    forward_start = protocol_event(
        "session_started",
        forward_source["session_id"],
        1,
        now - dt.timedelta(seconds=10, milliseconds=500),
        expectations,
        active_path_id=derivative_path_id,
        save_hash=initial_hash,
        reason="save_loader_load_complete",
    )
    forward_save_records = save_protocol_records(
        forward_source["session_id"],
        7,
        now - dt.timedelta(seconds=8, milliseconds=200),
        expectations,
        active_path_before=derivative_path_id,
        target_path=derivative_path_id,
        before_hash=initial_hash,
        after_hash=forward_save_hash,
        control_id="save-priority-forward",
        completed=forward_save,
    )
    forward_reload_records = reload_protocol_records(
        forward_source["session_id"],
        10,
        now - dt.timedelta(seconds=7, milliseconds=900),
        expectations,
        active_path_id=derivative_path_id,
        save_hash=forward_save_hash,
        control_id="reload-priority-forward",
    )
    inverse_start = protocol_event(
        "session_started",
        inverse_source["session_id"],
        1,
        now - dt.timedelta(seconds=7, milliseconds=500),
        expectations,
        active_path_id=derivative_path_id,
        save_hash=forward_save_hash,
        reason="save_loader_load_complete",
    )
    inverse_save_records = save_protocol_records(
        inverse_source["session_id"],
        7,
        now - dt.timedelta(seconds=5, milliseconds=200),
        expectations,
        active_path_before=derivative_path_id,
        target_path=derivative_path_id,
        before_hash=forward_save_hash,
        after_hash=inverse_save_hash,
        control_id="save-priority-inverse",
        completed=inverse_save,
    )
    inverse_reload_records = reload_protocol_records(
        inverse_source["session_id"],
        10,
        now - dt.timedelta(seconds=4, milliseconds=900),
        expectations,
        active_path_id=derivative_path_id,
        save_hash=inverse_save_hash,
        control_id="reload-priority-inverse",
    )
    clean_records = [
        origin_start,
        reservation_source,
        *origin_save_records,
        *origin_reload_records,
        forward_start,
        forward_source,
        *forward_records,
        forward_witness,
        *forward_save_records,
        *forward_reload_records,
        inverse_start,
        inverse_source,
        *inverse_records,
        inverse_witness,
        *inverse_save_records,
        *inverse_reload_records,
        *final_session_records,
    ]
    state_log = bridge / "state.jsonl"
    state_log.unlink()
    append_records(state_log, *clean_records)
    for archived_log in (bridge / "sessions").glob("*/state.jsonl"):
        archived_log.unlink()
    oni_bridge.archive_session_records(
        bridge,
        copy.deepcopy(clean_records),
        rebuild=True,
    )
    terminal_runtime = oni_bridge.ReplayState(
        startup_expectations=copy.deepcopy(expectations),
        require_startup_binding=True,
    )
    original_snapshot_trust_blockers = oni_bridge.snapshot_trust_blockers

    def trust_at_fixture_arrival(state: Dict[str, Any]) -> list[str]:
        observed_at = dt.datetime.fromisoformat(str(state.get("ts") or "").replace("Z", "+00:00"))
        return original_snapshot_trust_blockers(state, now=observed_at)

    # This fixture represents records processed live at their observation
    # times, then replayed by the finalizer after the origin snapshot is old.
    # A scoped clock substitution avoids changing raw timestamps or leaking a
    # global patch outside this single-threaded construction step.
    with mock.patch.object(
        oni_bridge,
        "snapshot_trust_blockers",
        side_effect=trust_at_fixture_arrival,
    ):
        terminal_runtime.process_many(copy.deepcopy(clean_records))
    require(
        terminal_runtime.startup_integrity_blockers == set()
        and terminal_runtime.session_integrity_blockers == set()
        and terminal_runtime.current_session_id == final_reload["session_id"]
        and terminal_runtime.session_ended is True,
        f"fixture startup replay did not reach a clean terminal state: "
        f"{sorted(terminal_runtime.startup_integrity_blockers | terminal_runtime.session_integrity_blockers)}",
    )
    # Exercise the production terminal publication path in an isolated Bridge
    # output root so this focused fixture does not replay the deliberately
    # pruned command-journal adversaries above.  Copy the exact resulting
    # publication set into the lineage root; no enriched object is hand-built.
    terminal_output = root / "terminal-publication"
    write_json_atomic(
        terminal_output / "action-policy.json",
        load_json(bridge / "action-policy.json"),
    )
    for command_id in (forward_command["id"], inverse_command["id"]):
        destination = terminal_output / "commands" / "done" / f"{command_id}.json"
        write_json_atomic(
            destination,
            load_json(bridge / "commands" / "done" / f"{command_id}.json"),
        )
    oni_bridge.write_outputs(terminal_output, terminal_runtime, replay_mode=False)
    for output_name in (
        "latest_state.json",
        "health.json",
        "evidence-continuity.json",
        "action-outcomes.json",
        "decision-frame.json",
        "strategy.json",
        "advice.json",
        "incidents.json",
        "plan.json",
    ):
        shutil.copyfile(terminal_output / output_name, bridge / output_name)
    checkpoint(
        argparse.Namespace(
            manifest=manifest_path,
            bridge_dir=bridge,
            step_id="priority_inverse",
            scenario="confirmed inverse priority 4 to 2 persists after explicit save/reload",
            outcome="confirmed",
            expected_mutation="persisted_change",
            command_id=inverse_command["id"],
            evidence=[inverse_freeze["path"]],
        )
    )
    return {
        "root": root,
        "bridge": bridge,
        "manifest": manifest_path,
        "forward_id": forward_command["id"],
        "inverse_id": inverse_command["id"],
        "forward_frame_id": forward_frame["frame_id"],
        "inverse_frame_id": inverse_frame["frame_id"],
        "forward_publication_write_blocked": forward_confirmation["publication_write_blocked"],
        "forward_missing_frame_blocked": forward_confirmation["missing_frame_blocked"],
        "forward_conflicting_bundle_blocked": forward_confirmation[
            "conflicting_bundle_blocked"
        ],
        "forward_bundle_hash": forward_confirmation["bundle"]["bundle_hash"],
        "inverse_bundle_hash": inverse_confirmation["bundle"]["bundle_hash"],
        "final_raw_tip": final_raw,
        "origin_snapshot_ts": reservation_source["ts"],
        "origin_snapshot_id": reservation_source["snapshot_id"],
    }


def expect_rejected(manifest: pathlib.Path, bridge: pathlib.Path, label: str) -> str:
    try:
        verify_priority_pair(manifest, bridge)
    except PriorityPairError as exc:
        return exc.code
    raise AssertionError(f"{label} fixture unexpectedly passed")


def reset_fixture(pristine: pathlib.Path, target: pathlib.Path) -> Dict[str, pathlib.Path]:
    """Restore bytes at the same absolute paths so manifest path ids stay valid."""

    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(pristine, target)
    manifest = target / "bridge" / "evidence" / "save-lineage" / "priority-pair-fixture" / "manifest.json"
    return {"root": target, "bridge": target / "bridge", "manifest": manifest}


def write_manifest(path: pathlib.Path, manifest: Dict[str, Any], *, rehash: bool = False) -> None:
    if rehash:
        rehash_checkpoints(manifest)
        checkpoints = manifest.get("checkpoints") or []
        if checkpoints:
            manifest["latest"] = copy.deepcopy(checkpoints[-1]["reload"])
    write_json_atomic(path, manifest)


def rewrite_log_record(
    path: pathlib.Path,
    predicate: Any,
    mutator: Any,
) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    matches = 0
    rewritten: list[str] = []
    for line in lines:
        record = json.loads(line)
        if predicate(record):
            matches += 1
            mutator(record)
        rewritten.append(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    require(matches == 1, f"fixture log mutation matched {matches} records")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8", newline="\n")


def rewrite_log_record_nonfinite(
    path: pathlib.Path,
    predicate: Any,
) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    matches = 0
    rewritten: list[str] = []
    for line in lines:
        record = json.loads(line)
        if predicate(record):
            matches += 1
            record["writer"]["queue_depth"] = float("nan")
        rewritten.append(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=True,
            )
        )
    require(matches == 1, f"fixture nonfinite mutation matched {matches} records")
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8", newline="\n")


def run_fixture() -> Dict[str, Any]:
    global ASSERTION_COUNT
    ASSERTION_COUNT = 0
    with tempfile.TemporaryDirectory(prefix="oni-priority-pair-") as temporary:
        workspace = pathlib.Path(temporary)
        valid_root = workspace / "valid"
        valid_root.mkdir()
        fixture = build_valid_fixture(valid_root)
        report = verify_priority_pair(fixture["manifest"], fixture["bridge"])
        require(report["status"] == "passed", "valid priority pair did not pass")
        require(len(report["pair"]["commands"]) == 2, "final report did not freeze exactly two commands")
        pipeline_report = report.get("shutdown", {}).get("command_pipeline", {})
        latest_state = load_json(fixture["bridge"] / "latest_state.json")
        health = load_json(fixture["bridge"] / "health.json")
        raw_tip = fixture["final_raw_tip"]
        health_published_at = dt.datetime.fromisoformat(str(health["updated_at"]))
        origin_observed_at = dt.datetime.fromisoformat(
            str(fixture["origin_snapshot_ts"]).replace("Z", "+00:00")
        )
        historical_snapshot_age_seconds = (
            health_published_at - origin_observed_at
        ).total_seconds()
        require(
            historical_snapshot_age_seconds >= 30.0
            and "untrusted_snapshot:snapshot_stale" not in health.get("warnings", []),
            "valid long-run fixture did not preserve clean live-time warnings",
        )
        final_policy = load_json(fixture["bridge"] / "action-policy.json")
        expected_pipeline = command_pipeline_readiness(
            fixture["bridge"],
            latest_state=latest_state,
            policy=final_policy,
        )
        freshness = pipeline_report.get("inputs", {}).get("snapshot_freshness", {})
        checked_at = dt.datetime.fromisoformat(str(freshness.get("checked_at")))
        expected_age, expected_threshold, expected_freshness_blockers = snapshot_freshness(
            latest_state,
            now=checked_at,
        )
        expected_structural_blockers = [
            blocker
            for blocker in expected_freshness_blockers
            if blocker != "snapshot_stale"
        ]
        require(
            pipeline_report.get("schema") == COMMAND_PIPELINE_SCHEMA
            and pipeline_report.get("gate_hash") == canonical_hash(expected_pipeline)
            and pipeline_report.get("mutation_proposal") == {
                "ready": True,
                "blockers": [],
            }
            and pipeline_report.get("inputs", {}).get("latest_state_hash")
            == canonical_hash(latest_state)
            and pipeline_report.get("inputs", {}).get("latest_state_core_hash")
            == canonical_state_hash(raw_tip)
            and pipeline_report.get("inputs", {}).get("health_file_sha256")
            == sha256_file(fixture["bridge"] / "health.json")
            and pipeline_report.get("inputs", {}).get("health_status") == "session_ended"
            and pipeline_report.get("inputs", {}).get("terminal_publication") is True
            and pipeline_report.get("command_count") == 0,
            "final report did not bind the clean command-pipeline gate",
        )
        require(
            "bridge" not in raw_tip
            and latest_state.get("bridge", {}).get("trust_ready") is False
            and health.get("status") == "session_ended"
            and health.get("trust_ready") is False
            and pair_workflow.snapshot_core_without_bridge(latest_state) == raw_tip,
            "valid fixture did not model a real terminal Bridge publication",
        )
        require(
            freshness
            == {
                "checked_at": checked_at.isoformat(timespec="microseconds"),
                "snapshot_ts": latest_state["ts"],
                "sample_interval_seconds": latest_state["sample"]["interval_seconds"],
                "age_seconds": expected_age,
                "threshold_seconds": pair_workflow.PAIR_CREATION_MAX_STATE_AGE_SECONDS,
                "blockers": expected_structural_blockers,
            }
            and expected_structural_blockers == []
            and expected_age is not None
            and expected_threshold is not None
            and expected_age <= pair_workflow.PAIR_CREATION_MAX_STATE_AGE_SECONDS,
            "final report did not freeze an exact creation-time freshness proof",
        )

        observed_at = dt.datetime.fromisoformat(str(latest_state["ts"]).replace("Z", "+00:00"))
        original_utc_now = pair_workflow._utc_now
        pair_freshness_boundaries: Dict[str, str] = {}
        try:
            pair_workflow._utc_now = lambda: observed_at + dt.timedelta(seconds=60)
            within_cleanup_window = pair_workflow._command_pipeline_evidence(
                fixture["bridge"], latest_state, final_policy, raw_state=raw_tip
            )
            require(
                within_cleanup_window["inputs"]["snapshot_freshness"]
                == {
                    "checked_at": (observed_at + dt.timedelta(seconds=60)).isoformat(
                        timespec="microseconds"
                    ),
                    "snapshot_ts": latest_state["ts"],
                    "sample_interval_seconds": latest_state["sample"]["interval_seconds"],
                    "age_seconds": 60.0,
                    "threshold_seconds": pair_workflow.PAIR_CREATION_MAX_STATE_AGE_SECONDS,
                    "blockers": [],
                },
                "pair cleanup ceiling did not accept and freeze age above sample*3",
            )
            pair_freshness_boundaries["age_above_sample_threshold_below_120"] = "accepted"

            for label, state, current, expected_code in (
                (
                    "age_above_120",
                    latest_state,
                    observed_at + dt.timedelta(seconds=121),
                    "command_pipeline_snapshot_not_fresh",
                ),
                (
                    "future_timestamp",
                    latest_state,
                    observed_at - dt.timedelta(seconds=10),
                    "bridge_health_publication_from_future",
                ),
                (
                    "invalid_timestamp",
                    {**latest_state, "ts": "not-a-timestamp"},
                    observed_at,
                    "raw_tip_timestamp_invalid",
                ),
                (
                    "invalid_interval",
                    {
                        **latest_state,
                        "sample": {**latest_state["sample"], "interval_seconds": 0.0},
                    },
                    observed_at,
                    "raw_tip_snapshot_untrusted",
                ),
                (
                    "bridge_stale",
                    {
                        **latest_state,
                        "bridge": {
                            **latest_state["bridge"],
                            "stale": True,
                            "trust_ready": False,
                            "trust_blockers": ["snapshot_stale"],
                        },
                    },
                    observed_at,
                    "latest_state_bridge_projection_invalid",
                ),
                (
                    "forged_terminal_trust_ready",
                    {
                        **latest_state,
                        "bridge": {
                            **latest_state["bridge"],
                            "stale": False,
                            "trust_ready": True,
                            "trust_blockers": [],
                        },
                    },
                    observed_at,
                    "bridge_health_trust_mismatch",
                ),
                (
                    "bridge_trust_blockers_present",
                    {
                        **latest_state,
                        "bridge": {
                            **latest_state["bridge"],
                            "stale": False,
                            "trust_ready": True,
                            "trust_blockers": ["sequence_gap"],
                        },
                    },
                    observed_at,
                    "latest_state_bridge_projection_invalid",
                ),
            ):
                pair_workflow._utc_now = lambda current=current: current
                write_json_atomic(fixture["bridge"] / "latest_state.json", state)
                try:
                    pair_workflow._command_pipeline_evidence(
                        fixture["bridge"],
                        state,
                        final_policy,
                        raw_state=pair_workflow.snapshot_core_without_bridge(state),
                    )
                except PriorityPairError as exc:
                    require(exc.code == expected_code, f"{label} stopped at {exc.code}")
                    pair_freshness_boundaries[label] = exc.code
                else:
                    raise PriorityPairError("fixture_expected_rejection_missing", label)
                finally:
                    write_json_atomic(
                        fixture["bridge"] / "latest_state.json",
                        latest_state,
                    )
        finally:
            pair_workflow._utc_now = original_utc_now
        published_at = dt.datetime.fromisoformat(str(health["updated_at"]))
        raw_after_health = copy.deepcopy(raw_tip)
        enriched_after_health = copy.deepcopy(latest_state)
        forged_tip_ts = published_at + dt.timedelta(milliseconds=500)
        raw_after_health["ts"] = iso(forged_tip_ts)
        enriched_after_health["ts"] = iso(forged_tip_ts)
        raw_after_health_blocker = ""
        try:
            pair_workflow.validate_enriched_latest_against_raw(
                raw_after_health,
                enriched_after_health,
                health,
            )
        except PriorityPairError as exc:
            raw_after_health_blocker = exc.code
        require(
            raw_after_health_blocker == "raw_tip_after_health_publication",
            f"raw tip after health stopped at {raw_after_health_blocker}",
        )
        stale_raw = copy.deepcopy(raw_tip)
        stale_enriched = copy.deepcopy(latest_state)
        stale_health = copy.deepcopy(health)
        stale_age_seconds = 30.0
        stale_tip_ts = published_at - dt.timedelta(seconds=stale_age_seconds)
        stale_raw["ts"] = iso(stale_tip_ts)
        stale_enriched["ts"] = iso(stale_tip_ts)
        stale_enriched["bridge"]["snapshot_age_seconds"] = stale_age_seconds
        stale_health["snapshot_age_seconds"] = stale_age_seconds
        stale_health["source"]["state_hash"] = canonical_state_hash(stale_raw)
        latest_stale_blocker = ""
        try:
            pair_workflow.validate_enriched_latest_against_raw(
                stale_raw,
                stale_enriched,
                stale_health,
            )
        except PriorityPairError as exc:
            latest_stale_blocker = exc.code
        require(
            latest_stale_blocker == "raw_tip_snapshot_untrusted",
            f"stale final raw tip stopped at {latest_stale_blocker}",
        )
        report_path = fixture["manifest"].parent / "priority-pair-finalization.json"
        write_report_create_only(report_path, report)
        immutable_report_rejected = False
        try:
            write_report_create_only(report_path, report)
        except PriorityPairError as exc:
            immutable_report_rejected = exc.code == "finalization_report_already_exists"
        require(immutable_report_rejected, "create-only final report was overwritten")

        pristine = workspace / "pristine"
        shutil.copytree(valid_root, pristine)
        case = reset_fixture(pristine, valid_root)
        rewrite_log_record(
            case["bridge"] / "state.jsonl",
            lambda record: record.get("snapshot_id")
            == fixture["final_raw_tip"]["snapshot_id"],
            lambda record: record.__setitem__(
                "bridge",
                {
                    "stale": False,
                    "snapshot_age_seconds": 0.0,
                    "trust_ready": True,
                    "trust_blockers": [],
                    "replay_mode": False,
                },
            ),
        )
        raw_bridge_report = verify_priority_pair(case["manifest"], case["bridge"])
        require(
            raw_bridge_report.get("status") == "passed"
            and raw_bridge_report.get("shutdown", {})
            .get("command_pipeline", {})
            .get("inputs", {})
            .get("terminal_publication")
            is True,
            "raw Bridge self-assertion affected the trusted derived publication",
        )
        raw_bridge_ignored = True
        rejected: Dict[str, str] = {}

        case = reset_fixture(pristine, valid_root)
        rewrite_log_record(
            case["bridge"] / "state.jsonl",
            lambda record: record.get("snapshot_id") == fixture["origin_snapshot_id"],
            lambda record: record.__setitem__("ts", "not-a-timestamp"),
        )
        rejected["historical_timestamp_invalid"] = expect_rejected(
            case["manifest"], case["bridge"], "historical timestamp invalid"
        )
        require(
            rejected["historical_timestamp_invalid"]
            == "bridge_replay_historical_snapshot_untrusted",
            "historical invalid timestamp stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        case_health = load_json(case["bridge"] / "health.json")
        historical_future_ts = dt.datetime.fromisoformat(
            str(case_health["updated_at"])
        ) + dt.timedelta(seconds=1)
        rewrite_log_record(
            case["bridge"] / "state.jsonl",
            lambda record: record.get("snapshot_id") == fixture["origin_snapshot_id"],
            lambda record: record.__setitem__("ts", iso(historical_future_ts)),
        )
        rejected["historical_timestamp_after_publication"] = expect_rejected(
            case["manifest"], case["bridge"], "historical timestamp after publication"
        )
        require(
            rejected["historical_timestamp_after_publication"]
            == "bridge_replay_historical_snapshot_after_publication",
            "historical future timestamp stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        rewrite_log_record(
            case["bridge"] / "state.jsonl",
            lambda record: record.get("snapshot_id") == fixture["origin_snapshot_id"],
            lambda record: record["sample"].__setitem__("interval_seconds", 0.0),
        )
        rejected["historical_sample_interval_invalid"] = expect_rejected(
            case["manifest"], case["bridge"], "historical sample interval invalid"
        )
        require(
            rejected["historical_sample_interval_invalid"]
            == "bridge_replay_historical_snapshot_untrusted",
            "historical invalid sample interval stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        rewrite_log_record(
            case["bridge"] / "state.jsonl",
            lambda record: record.get("snapshot_id") == fixture["origin_snapshot_id"],
            lambda record: record["writer"].__setitem__("fault_count", 1),
        )
        rejected["historical_writer_fault"] = expect_rejected(
            case["manifest"], case["bridge"], "historical writer fault"
        )
        require(
            rejected["historical_writer_fault"]
            == "bridge_replay_historical_snapshot_untrusted",
            "historical writer fault stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        rewrite_log_record_nonfinite(
            case["bridge"] / "state.jsonl",
            lambda record: record.get("snapshot_id") == fixture["origin_snapshot_id"],
        )
        rejected["historical_nonfinite_json"] = expect_rejected(
            case["manifest"], case["bridge"], "historical nonfinite JSON"
        )
        require(
            rejected["historical_nonfinite_json"]
            == "bridge_state_log_record_invalid",
            "historical nonfinite JSON stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        stale_warning_health = load_json(case["bridge"] / "health.json")
        stale_warning_health["warnings"].append("untrusted_snapshot:snapshot_stale")
        write_json_atomic(case["bridge"] / "health.json", stale_warning_health)
        rejected["historical_stale_warning_reported"] = expect_rejected(
            case["manifest"], case["bridge"], "historical stale warning reported"
        )
        require(
            rejected["historical_stale_warning_reported"]
            == "bridge_health_historical_stale_warning",
            "reported historical stale warning stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        freeze_path = (
            case["manifest"].parent / "priority-command-freezes" / "priority_forward.json"
        )
        freeze = load_json(freeze_path)
        freeze["approval_bundle"]["document"]["source_frame"]["document"]["trust"]["status"] = "blocked"
        write_json_atomic(freeze_path, freeze)
        rejected["tampered"] = expect_rejected(case["manifest"], case["bridge"], "tampered")

        case = reset_fixture(pristine, valid_root)
        (case["manifest"].parent / "priority-command-freezes" / "priority_inverse.json").unlink()
        rejected["missing"] = expect_rejected(case["manifest"], case["bridge"], "missing")

        case = reset_fixture(pristine, valid_root)
        manifest = load_json(case["manifest"])
        manifest["checkpoints"] = list(reversed(manifest["checkpoints"]))
        write_json_atomic(case["manifest"], manifest)
        rejected["swapped"] = expect_rejected(case["manifest"], case["bridge"], "swapped")

        case = reset_fixture(pristine, valid_root)
        inverse_path = case["bridge"] / "commands" / "done" / f"{fixture['inverse_id']}.json"
        inverse = load_json(inverse_path)
        inverse["args"]["expected_name"] = "DifferentAda"
        write_json_atomic(inverse_path, inverse)
        rejected["different_target"] = expect_rejected(case["manifest"], case["bridge"], "different target")

        case = reset_fixture(pristine, valid_root)
        inverse_path = case["bridge"] / "commands" / "done" / f"{fixture['inverse_id']}.json"
        inverse = load_json(inverse_path)
        inverse["args"]["priority"] = 3
        write_json_atomic(inverse_path, inverse)
        rejected["not_inverse"] = expect_rejected(case["manifest"], case["bridge"], "not inverse")

        case = reset_fixture(pristine, valid_root)
        inverse_bundle_path = confirmed_command_approval_bundle_path(case["bridge"], fixture["inverse_id"])
        inverse_bundle = load_json(inverse_bundle_path)
        forward_bundle = load_json(confirmed_command_approval_bundle_path(case["bridge"], fixture["forward_id"]))
        inverse_bundle["source_frame"] = copy.deepcopy(forward_bundle["source_frame"])
        write_json_atomic(inverse_bundle_path, inverse_bundle)
        rejected["old_frame"] = expect_rejected(case["manifest"], case["bridge"], "old frame")

        case = reset_fixture(pristine, valid_root)
        original = case["bridge"] / "commands" / "done" / f"{fixture['forward_id']}.json"
        duplicate = case["bridge"] / "commands" / "failed" / "duplicate-forward.json"
        duplicate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, duplicate)
        rejected["duplicate_id"] = expect_rejected(case["manifest"], case["bridge"], "duplicate id")

        case = reset_fixture(pristine, valid_root)
        pending = case["bridge"] / "commands" / "pending" / "unrelated.json"
        write_json_atomic(pending, {"id": "unrelated", "status": "pending_human_approval"})
        rejected["queue_nonempty"] = expect_rejected(case["manifest"], case["bridge"], "queue nonempty")

        case = reset_fixture(pristine, valid_root)
        temporary_path = case["bridge"] / "commands" / "pending" / "orphan.tmp"
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text("partial", encoding="utf-8")
        rejected["temp_nonempty"] = expect_rejected(case["manifest"], case["bridge"], "temp nonempty")

        case = reset_fixture(pristine, valid_root)
        state_log = case["bridge"] / "state.jsonl"
        lines = state_log.read_text(encoding="utf-8").splitlines()
        final_index = next(
            index
            for index, line in enumerate(lines)
            if json.loads(line).get("snapshot_id") == fixture["final_raw_tip"]["snapshot_id"]
        )
        final = json.loads(lines[final_index])
        final["duplicants"]["by_instance_id"][f"id_{INSTANCE_ID}"]["chore_priorities"][CHORE_GROUP] = 3
        lines[final_index] = json.dumps(final, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        state_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rejected["final_value"] = expect_rejected(case["manifest"], case["bridge"], "final value")

        case = reset_fixture(pristine, valid_root)
        enriched = load_json(case["bridge"] / "latest_state.json")
        enriched["game"]["paused"] = False
        write_json_atomic(case["bridge"] / "latest_state.json", enriched)
        rejected["enriched_core_drift"] = expect_rejected(
            case["manifest"], case["bridge"], "enriched core drift"
        )
        require(
            rejected["enriched_core_drift"] == "latest_state_core_mismatch",
            "enriched core drift stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        enriched = load_json(case["bridge"] / "latest_state.json")
        enriched["bridge"]["forged"] = True
        write_json_atomic(case["bridge"] / "latest_state.json", enriched)
        rejected["forged_bridge_shape"] = expect_rejected(
            case["manifest"], case["bridge"], "forged bridge shape"
        )
        require(
            rejected["forged_bridge_shape"] == "latest_state_bridge_fields_invalid",
            "forged Bridge shape stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        forged_health = load_json(health_path)
        forged_health["source"]["snapshot_id"] = "forged-snapshot"
        write_json_atomic(health_path, forged_health)
        rejected["forged_health_source"] = expect_rejected(
            case["manifest"], case["bridge"], "forged health source"
        )
        require(
            rejected["forged_health_source"] == "bridge_health_source_mismatch",
            "forged health source stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        forged_health = load_json(health_path)
        forged_health["forged"] = True
        write_json_atomic(health_path, forged_health)
        rejected["health_extra_field"] = expect_rejected(
            case["manifest"], case["bridge"], "health extra field"
        )
        require(
            rejected["health_extra_field"] == "bridge_health_fields_invalid",
            "health extra field stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        forged_health = load_json(health_path)
        forged_health.pop("session_start_observed")
        write_json_atomic(health_path, forged_health)
        rejected["health_missing_field"] = expect_rejected(
            case["manifest"], case["bridge"], "health missing field"
        )
        require(
            rejected["health_missing_field"] == "bridge_health_fields_invalid",
            "health missing field stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        forged_health = load_json(health_path)
        forged_health["startup_binding"]["required"] = False
        write_json_atomic(health_path, forged_health)
        rejected["startup_not_required"] = expect_rejected(
            case["manifest"], case["bridge"], "startup binding not required"
        )
        require(
            rejected["startup_not_required"]
            == "bridge_health_startup_required_invalid",
            "startup required=false stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        forged_health = load_json(health_path)
        forged_health["startup_binding"]["external_expectations_supplied"] = False
        write_json_atomic(health_path, forged_health)
        rejected["startup_external_missing"] = expect_rejected(
            case["manifest"], case["bridge"], "startup external expectations missing"
        )
        require(
            rejected["startup_external_missing"]
            == "bridge_health_startup_projection_invalid",
            "startup external=false stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        latest_path = case["bridge"] / "latest_state.json"
        forged_health = load_json(health_path)
        forged_latest = load_json(latest_path)
        forged_health["status"] = "ok"
        forged_health["trust_ready"] = True
        forged_latest["bridge"]["trust_ready"] = True
        write_json_atomic(health_path, forged_health)
        write_json_atomic(latest_path, forged_latest)
        rejected["nonterminal_publication"] = expect_rejected(
            case["manifest"], case["bridge"], "coordinated nonterminal publication"
        )
        require(
            rejected["nonterminal_publication"] == "bridge_terminal_publication_required",
            "coordinated nonterminal publication stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        forged_health = load_json(health_path)
        forged_health["duplicate_seq_count"] = 1
        write_json_atomic(health_path, forged_health)
        rejected["health_integrity_counter"] = expect_rejected(
            case["manifest"], case["bridge"], "health integrity counter"
        )
        require(
            rejected["health_integrity_counter"]
            == "bridge_health_integrity_blocker_present",
            "health integrity counter stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        forged_health = load_json(health_path)
        forged_health["warnings"].append("forged_clean_terminal")
        write_json_atomic(health_path, forged_health)
        rejected["health_warning"] = expect_rejected(
            case["manifest"], case["bridge"], "health warning forgery"
        )
        require(
            rejected["health_warning"] == "bridge_health_warning_replay_mismatch",
            "health warning forgery stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        health_path = case["bridge"] / "health.json"
        latest_path = case["bridge"] / "latest_state.json"
        forged_health = load_json(health_path)
        forged_latest = load_json(latest_path)
        forged_health["snapshot_age_seconds"] += 10.0
        forged_latest["bridge"]["snapshot_age_seconds"] += 10.0
        write_json_atomic(health_path, forged_health)
        write_json_atomic(latest_path, forged_latest)
        rejected["health_age"] = expect_rejected(
            case["manifest"], case["bridge"], "health age forgery"
        )
        require(
            rejected["health_age"] == "bridge_publication_age_mismatch",
            "health age forgery stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        state_log = case["bridge"] / "state.jsonl"
        lines = state_log.read_text(encoding="utf-8").splitlines()
        raw_index = next(
            index
            for index, line in enumerate(lines)
            if json.loads(line).get("snapshot_id") == fixture["final_raw_tip"]["snapshot_id"]
        )
        conflicting_raw = json.loads(lines[raw_index])
        conflicting_raw["writer"]["queue_depth"] = 1
        lines[raw_index] = json.dumps(
            conflicting_raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        state_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rejected["raw_writer_ambiguity"] = expect_rejected(
            case["manifest"], case["bridge"], "raw writer ambiguity"
        )
        require(
            rejected["raw_writer_ambiguity"] == "tip_snapshot_missing_or_ambiguous",
            "writer-only raw ambiguity was collapsed by the tip hash",
        )

        case = reset_fixture(pristine, valid_root)
        rewrite_log_record(
            case["bridge"] / "state.jsonl",
            lambda record: record.get("snapshot_id")
            == fixture["final_raw_tip"]["snapshot_id"],
            lambda record: record.__setitem__("read_metrics_ms", 999.0),
        )
        rejected["raw_read_metrics_ambiguity"] = expect_rejected(
            case["manifest"], case["bridge"], "raw read_metrics ambiguity"
        )
        require(
            rejected["raw_read_metrics_ambiguity"]
            == "tip_snapshot_missing_or_ambiguous",
            "read_metrics-only raw ambiguity was collapsed by the tip hash",
        )

        case = reset_fixture(pristine, valid_root)
        continuity_path = case["bridge"] / "evidence-continuity.json"
        forged_continuity = load_json(continuity_path)
        forged_continuity["recovered_record_count"] += 1
        write_json_atomic(continuity_path, forged_continuity)
        rejected["continuity_companion"] = expect_rejected(
            case["manifest"], case["bridge"], "continuity companion forgery"
        )
        require(
            rejected["continuity_companion"]
            == "bridge_health_evidence_continuity_mismatch",
            "continuity companion forgery stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        outcomes_path = case["bridge"] / "action-outcomes.json"
        forged_outcomes = load_json(outcomes_path)
        forged_outcomes["counts"]["executed_verified"] += 1
        write_json_atomic(outcomes_path, forged_outcomes)
        rejected["action_outcomes_companion"] = expect_rejected(
            case["manifest"], case["bridge"], "action outcomes companion forgery"
        )
        require(
            rejected["action_outcomes_companion"] == "action_outcomes_invalid",
            "action outcomes companion forgery stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        frame_path = case["bridge"] / "decision-frame.json"
        forged_frame = load_json(frame_path)
        forged_frame["frame_id"] = "sha256:" + "f" * 64
        write_json_atomic(frame_path, forged_frame)
        rejected["decision_frame_companion"] = expect_rejected(
            case["manifest"], case["bridge"], "decision frame companion forgery"
        )
        require(
            rejected["decision_frame_companion"] == "decision_frame_invalid",
            "decision frame companion forgery stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        strategy_path = case["bridge"] / "strategy.json"
        forged_strategy = load_json(strategy_path)
        forged_strategy["status"] = "ready"
        write_json_atomic(strategy_path, forged_strategy)
        rejected["strategy_companion"] = expect_rejected(
            case["manifest"], case["bridge"], "strategy companion forgery"
        )
        require(
            rejected["strategy_companion"] == "bridge_health_strategy_mismatch",
            "strategy companion forgery stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        terminal_archive_path = (
            case["bridge"]
            / "sessions"
            / oni_bridge.safe_session_name(fixture["final_raw_tip"]["session_id"])
            / "state.jsonl"
        )
        terminal_lines = terminal_archive_path.read_text(encoding="utf-8").splitlines()
        terminal_archive_path.write_text(
            "\n".join(terminal_lines[:-1]) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        rejected["terminal_tail_missing"] = expect_rejected(
            case["manifest"], case["bridge"], "terminal tail missing"
        )
        require(
            rejected["terminal_tail_missing"] == "terminal_archive_tail_invalid",
            "terminal tail missing stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        terminal_archive_path = (
            case["bridge"]
            / "sessions"
            / oni_bridge.safe_session_name(fixture["final_raw_tip"]["session_id"])
            / "state.jsonl"
        )
        late_snapshot = copy.deepcopy(fixture["final_raw_tip"])
        late_snapshot["seq"] = 5
        late_snapshot["snapshot_seq"] = 2
        late_snapshot["snapshot_id"] += ":late"
        late_snapshot["ts"] = iso(dt.datetime.now(dt.timezone.utc).astimezone())
        append_records(terminal_archive_path, late_snapshot)
        rejected["terminal_late_snapshot"] = expect_rejected(
            case["manifest"], case["bridge"], "terminal archive late snapshot"
        )
        require(
            rejected["terminal_late_snapshot"] == "terminal_tip_not_last_snapshot",
            "terminal late snapshot stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        original_companion = pair_workflow._bridge_companion_evidence

        def replace_health_after_validation(*args: Any, **kwargs: Any) -> Any:
            result = original_companion(*args, **kwargs)
            health_path = case["bridge"] / "health.json"
            write_json_atomic(health_path, load_json(health_path))
            return result

        with mock.patch.object(
            pair_workflow,
            "_bridge_companion_evidence",
            side_effect=replace_health_after_validation,
        ):
            rejected["health_toctou"] = expect_rejected(
                case["manifest"], case["bridge"], "health post-validation replacement"
            )
        require(
            rejected["health_toctou"] == "evidence_file_changed_after_validation",
            "health post-validation replacement stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        original_companion = pair_workflow._bridge_companion_evidence

        def change_state_log_after_replay(*args: Any, **kwargs: Any) -> Any:
            result = original_companion(*args, **kwargs)
            with (case["bridge"] / "state.jsonl").open("ab") as handle:
                handle.write(b"\n")
            return result

        with mock.patch.object(
            pair_workflow,
            "_bridge_companion_evidence",
            side_effect=change_state_log_after_replay,
        ):
            rejected["state_log_toctou"] = expect_rejected(
                case["manifest"], case["bridge"], "state log post-replay change"
            )
        require(
            rejected["state_log_toctou"] == "evidence_file_changed_after_validation",
            "state log post-replay change stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        original_companion = pair_workflow._bridge_companion_evidence

        def change_archive_after_validation(*args: Any, **kwargs: Any) -> Any:
            result = original_companion(*args, **kwargs)
            archive_path = (
                case["bridge"]
                / "sessions"
                / oni_bridge.safe_session_name(fixture["final_raw_tip"]["session_id"])
                / "state.jsonl"
            )
            with archive_path.open("ab") as handle:
                handle.write(b"\n")
            return result

        with mock.patch.object(
            pair_workflow,
            "_bridge_companion_evidence",
            side_effect=change_archive_after_validation,
        ):
            rejected["terminal_archive_toctou"] = expect_rejected(
                case["manifest"], case["bridge"], "terminal archive post-validation change"
            )
        require(
            rejected["terminal_archive_toctou"]
            == "evidence_file_changed_after_validation",
            "terminal archive post-validation change stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        original_companion = pair_workflow._bridge_companion_evidence

        def change_command_tree_after_gate(*args: Any, **kwargs: Any) -> Any:
            result = original_companion(*args, **kwargs)
            write_json_atomic(
                case["bridge"] / "commands" / "done" / "toctou-extra.json",
                {"id": "toctou-extra", "status": "done"},
            )
            return result

        with mock.patch.object(
            pair_workflow,
            "_bridge_companion_evidence",
            side_effect=change_command_tree_after_gate,
        ):
            rejected["command_tree_toctou"] = expect_rejected(
                case["manifest"], case["bridge"], "command tree post-gate change"
            )
        require(
            rejected["command_tree_toctou"] == "command_tree_changed_during_validation",
            "command tree post-gate change stopped at the wrong gate",
        )

        case = reset_fixture(pristine, valid_root)
        manifest = load_json(case["manifest"])
        manifest["checkpoints"] = manifest["checkpoints"][:1]
        manifest["latest"] = copy.deepcopy(manifest["checkpoints"][0]["reload"])
        manifest["latest_checkpoint_hash"] = manifest["checkpoints"][0]["checkpoint_hash"]
        write_json_atomic(case["manifest"], manifest)
        rejected["missing_checkpoint"] = expect_rejected(
            case["manifest"], case["bridge"], "missing checkpoint"
        )

        case = reset_fixture(pristine, valid_root)
        stale_state = load_json(case["bridge"] / "latest_state.json")
        stale_observed_at = dt.datetime.fromisoformat(
            str(stale_state["ts"]).replace("Z", "+00:00")
        )
        original_utc_now = pair_workflow._utc_now
        try:
            pair_workflow._utc_now = lambda: stale_observed_at + dt.timedelta(seconds=121)
            rejected["stale_at_pair_creation"] = expect_rejected(
                case["manifest"],
                case["bridge"],
                "stale snapshot at pair creation",
            )
        finally:
            pair_workflow._utc_now = original_utc_now
        require(
            rejected["stale_at_pair_creation"]
            == "command_pipeline_snapshot_not_fresh",
            f"stale pair snapshot stopped at {rejected['stale_at_pair_creation']}",
        )

        require(len(rejected) == 41 and all(rejected.values()), "not every adversarial case was rejected")

        case = reset_fixture(pristine, valid_root)
        manifest = load_json(case["manifest"])
        unresolved = load_json(
            case["bridge"] / "commands" / "done" / f"{fixture['forward_id']}.json"
        )
        unresolved["id"] = "cmd-priority-unresolved-outcome-fixture"
        unresolved["save_id_hash"] = manifest["latest"]["save_id_hash"]
        unresolved["status"] = "failed"
        unresolved["classification"] = "outcome_unknown"
        unresolved_result = (
            unresolved["result"] if isinstance(unresolved.get("result"), dict) else {}
        )
        unresolved_result["classification"] = "outcome_unknown"
        unresolved["result"] = unresolved_result
        write_json_atomic(
            case["bridge"] / "commands" / "failed" / "unresolved-outcome.json",
            unresolved,
        )
        outcome_unknown_blocker = expect_rejected(
            case["manifest"], case["bridge"], "relevant failed outcome_unknown"
        )
        require(
            outcome_unknown_blocker == "command_pipeline_mutation_not_ready",
            f"outcome_unknown stopped at the wrong gate: {outcome_unknown_blocker}",
        )

        semantic_blockers: Dict[str, str] = {}
        try:
            validate_priority_inverse(
                (INSTANCE_ID, EXPECTED_NAME, CHORE_GROUP, 2, 4),
                (INSTANCE_ID, "Other", CHORE_GROUP, 4, 2),
            )
        except PriorityPairError as exc:
            semantic_blockers["different_target"] = exc.code
        try:
            validate_priority_inverse(
                (INSTANCE_ID, EXPECTED_NAME, CHORE_GROUP, 2, 4),
                (INSTANCE_ID, EXPECTED_NAME, CHORE_GROUP, 4, 3),
            )
        except PriorityPairError as exc:
            semantic_blockers["not_inverse"] = exc.code
        require(
            semantic_blockers
            == {
                "different_target": "priority_targets_differ",
                "not_inverse": "priority_commands_not_inverse",
            },
            "pair semantic blockers did not fail at their narrow gates",
        )
        return {
            "schema": "oni.priority-pair-lineage-fixture-verification.v1",
            "status": "passed",
            "valid_pair": report["status"],
            "automatic_pre_done_bundle": True,
            "publication_write_failure_blocked": fixture["forward_publication_write_blocked"],
            "missing_frame_commit_blocked": fixture["forward_missing_frame_blocked"],
            "conflicting_bundle_commit_blocked": fixture[
                "forward_conflicting_bundle_blocked"
            ],
            "same_round_prune_survived": True,
            "create_only_report_rejected_overwrite": immutable_report_rejected,
            "command_pipeline_gate_bound": True,
            "command_pipeline_gate_hash": pipeline_report["gate_hash"],
            "command_pipeline_counts": pipeline_report["counts"],
            "command_pipeline_creation_freshness": freshness,
            "command_pipeline_freshness_boundaries": pair_freshness_boundaries,
            "historical_snapshot_age_seconds": historical_snapshot_age_seconds,
            "historical_stale_warning_absent": True,
            "latest_stale_blocker": latest_stale_blocker,
            "raw_tip_after_health_blocker": raw_after_health_blocker,
            "raw_bridge_self_assertion_ignored": raw_bridge_ignored,
            "adversarial_rejection_count": len(rejected),
            "adversarial_blockers": rejected,
            "relevant_failed_outcome_unknown_blocker": outcome_unknown_blocker,
            "pair_semantic_blockers": semantic_blockers,
            "forward_bundle_hash": fixture["forward_bundle_hash"],
            "inverse_bundle_hash": fixture["inverse_bundle_hash"],
            "final_report_hash": report["report_hash"],
            "assertion_count": ASSERTION_COUNT,
        }


def main() -> int:
    result = run_fixture()
    result["report_hash"] = canonical_hash(result)
    report_path = PROJECT_ROOT / "reports" / "priority-pair-lineage-verification.json"
    write_json_atomic(report_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"Report: {report_path}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
