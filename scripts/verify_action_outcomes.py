#!/usr/bin/env python3
"""Offline adversarial checks for independent action-outcome attestation."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
from typing import Any, Callable, Dict, Mapping


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_outcomes import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_REJECTED,
    CONFIRMATION_WAITING,
    ActionOutcomeError,
    build_action_outcomes,
    command_intent_hash,
    read_action_outcome,
    validate_action_outcomes,
)
from agent.command_queue import COMMAND_SCHEMA, ensure_command_dirs, write_json_atomic
from agent.canonical_hash import canonical_hash
from agent.field_contracts import (
    GAME_PAUSED_CONTRACT_ID,
    build_condition,
    contract_quality_fixture,
)
from agent.state_quality import canonical_state_hash


SAVE_HASH = "sha256:" + "a" * 64
OTHER_SAVE_HASH = "sha256:" + "b" * 64
SOURCE_FRAME_HASH = "sha256:" + "c" * 64
POLICY_HASH = "sha256:" + "d" * 64
SESSION_ID = "session-action-outcome-fixture"
WORLD_ID = 0
SOURCE_SEQ = 10
ACCEPTED_ACK_SEQ = 11
EXECUTED_ACK_SEQ = 12
LOCAL_EFFECT_ACK_SEQ = 13
WITNESS_SEQ = 14
ATTEMPT_ID = "attempt-" + "e" * 32


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def document_hash(document: Mapping[str, Any], hash_field: str) -> str:
    value = copy.deepcopy(dict(document))
    value.pop(hash_field, None)
    return canonical_hash(value)


def make_snapshot(
    *,
    seq: int,
    paused: bool,
    timestamp: dt.datetime,
    snapshot_seq: int | None = None,
    session_id: str = SESSION_ID,
    save_hash: str = SAVE_HASH,
    world_id: int = WORLD_ID,
) -> Dict[str, Any]:
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": iso(timestamp),
        "session_id": session_id,
        "seq": seq,
        "snapshot_seq": seq if snapshot_seq is None else snapshot_seq,
        "snapshot_id": f"{session_id}:{seq}",
        "game_build": "U59-action-outcome-fixture",
        "mod_version": "0.8.0-action-outcome-fixture",
        "save_id_hash": save_hash,
        "save_dirty": False,
        "world_id": world_id,
        "observation_scope": {
            "active_world_id": world_id,
            "populated_world_ids": [world_id],
            "covered_world_ids": [world_id],
            "complete_for_populated_worlds": True,
        },
        "game": {"paused": paused},
        "quality": {
            "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
        },
        "sample": {"interval_seconds": 5.0, "duration_ms": 0.25},
        "writer": {
            "queue_depth": 0,
            "dropped_snapshot_count": 0,
            "fault_count": 0,
            "non_finite_numeric_count": 0,
        },
    }


def direct_state(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(dict(snapshot))
    value["capture_source"] = "executor_direct_read"
    return value


def make_approval_receipt(command: Mapping[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema": "oni.approval-receipt.v1",
        "command_id": command["id"],
        "command_intent_hash": command_intent_hash(command),
        "source_frame_id": command["source_frame_id"],
        "source": {
            "session_id": command["session_id"],
            "save_id_hash": command["save_id_hash"],
            "world_id": command["world_id"],
            "snapshot_id": command["source_snapshot_id"],
            "state_hash": command["state_hash"],
        },
        "policy_hash": POLICY_HASH,
        "approved_by": command["approved_by"],
        "approved_at": command["approved_at"],
    }
    payload["receipt_hash"] = canonical_hash(payload)
    return payload


def fixture(command_id: str) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    current = dt.datetime.now(dt.timezone.utc).astimezone()
    source = make_snapshot(
        seq=SOURCE_SEQ,
        snapshot_seq=1,
        paused=False,
        timestamp=current - dt.timedelta(seconds=9),
    )
    witness = make_snapshot(
        seq=WITNESS_SEQ,
        snapshot_seq=2,
        paused=True,
        timestamp=current - dt.timedelta(seconds=1),
    )
    created_at = current - dt.timedelta(seconds=8)
    approved_at = current - dt.timedelta(seconds=7)
    completed_at = current - dt.timedelta(seconds=3)
    expires_at = current + dt.timedelta(seconds=30)
    command: Dict[str, Any] = {
        "schema": COMMAND_SCHEMA,
        "id": command_id,
        "mode": "suggested",
        "status": "executed_pending_snapshot",
        "action": "set_paused",
        "args": {"paused": True},
        "session_id": source["session_id"],
        "save_id_hash": source["save_id_hash"],
        "world_id": source["world_id"],
        "source_incident_id": "action-outcome-fixture",
        "source_snapshot_id": source["snapshot_id"],
        "state_hash": canonical_state_hash(source),
        "source_frame_id": SOURCE_FRAME_HASH,
        "created_at": iso(created_at),
        "expires_at": iso(expires_at),
        "risk": "low",
        "reason": "Prove that a later Probe snapshot independently confirms the effect.",
        "evidence": {"game.paused": False},
        "preconditions": [
            build_condition(
                "game.paused",
                "eq",
                False,
                GAME_PAUSED_CONTRACT_ID,
                quality=("observed",),
            )
        ],
        "expected_effects": [
            build_condition(
                "game.paused",
                "eq",
                True,
                GAME_PAUSED_CONTRACT_ID,
                quality=("observed",),
            )
        ],
        "requires_approval": True,
        "source": {
            "kind": "rule_incident",
            "title": "Action outcome fixture",
            "incident_opened_at": iso(created_at),
            "state_seq": source["seq"],
            "state_ts": source["ts"],
            "policy_hash": POLICY_HASH,
        },
        "approved_at": iso(approved_at),
        "approved_by": "local_human",
        "attempt_id": ATTEMPT_ID,
        "accepted_ack_seq": ACCEPTED_ACK_SEQ,
        "executed_ack_seq": EXECUTED_ACK_SEQ,
        "local_effect_ack_seq": LOCAL_EFFECT_ACK_SEQ,
        "completed_at": iso(completed_at),
        "result": {
            "before_state": direct_state(source),
            "after_state": direct_state(witness),
        },
    }
    command["approval_receipt"] = make_approval_receipt(command)
    return source, witness, command


def health_for(
    witness: Mapping[str, Any],
    *,
    status: str = "ok",
    trust_ready: bool = True,
    blockers: list[str] | None = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "trust_ready": trust_ready,
        "trust_blockers": list(blockers or []),
        "current_session_id": witness.get("session_id"),
        "latest_snapshot_id": witness.get("snapshot_id"),
        "latest_seq": witness.get("seq"),
        "source": {
            "session_id": witness.get("session_id"),
            "snapshot_id": witness.get("snapshot_id"),
            "state_hash": canonical_state_hash(dict(witness)),
        },
    }


def make_command_ack(
    source: Mapping[str, Any],
    command: Mapping[str, Any],
    status: str,
) -> Dict[str, Any]:
    stage_sequences = {
        "accepted": int(command["accepted_ack_seq"]),
        "executed": int(command["executed_ack_seq"]),
        "local_effect_observed": int(command["local_effect_ack_seq"]),
    }
    stage_offsets = {
        "accepted": 3,
        "executed": 4,
        "local_effect_observed": 5,
    }
    messages = {
        "accepted": "Static policy and current-state preconditions passed.",
        "executed": "Pause state mutation was dispatched.",
        "local_effect_observed": (
            "Executor direct-read matches every expected effect; awaiting a later trusted snapshot."
        ),
    }
    source_at = dt.datetime.fromisoformat(str(source["ts"]))
    return {
        "schema": "oni.event.v2",
        "event": "command_ack",
        "ts": iso(source_at + dt.timedelta(seconds=stage_offsets[status])),
        "session_id": source["session_id"],
        "seq": stage_sequences[status],
        "game_build": source["game_build"],
        "mod_version": source["mod_version"],
        "save_id_hash": source["save_id_hash"],
        "save_dirty": source["save_dirty"],
        "world_id": source["world_id"],
        "id": command["id"],
        "action": command["action"],
        "status": status,
        "stage": status,
        "message": messages[status],
        "attempt_id": command["attempt_id"],
    }


def write_session_history(
    root: pathlib.Path,
    source: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    ack_statuses: tuple[str, ...] = ("accepted", "executed", "local_effect_observed"),
) -> None:
    path = root / "sessions" / str(source["session_id"]) / "state.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [dict(source)] + [
        make_command_ack(source, command, status)
        for status in ack_statuses
    ]
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def write_command(
    root: pathlib.Path,
    command: Mapping[str, Any],
    directory: str = "confirming",
) -> None:
    target = ensure_command_dirs(root)[directory] / f"{command['id']}.json"
    write_json_atomic(target, dict(command))


def prepare(
    root: pathlib.Path,
    command_id: str,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    source, witness, command = fixture(command_id)
    write_session_history(root, source, command)
    write_command(root, command)
    return source, witness, command


def only_outcome(document: Mapping[str, Any]) -> Dict[str, Any]:
    outcomes = document.get("outcomes")
    require(isinstance(outcomes, list) and len(outcomes) == 1, "expected exactly one action outcome")
    outcome = outcomes[0]
    require(isinstance(outcome, dict), "action outcome is not an object")
    return outcome


def assert_status(document: Mapping[str, Any], status: str) -> Dict[str, Any]:
    validate_action_outcomes(document)
    outcome = only_outcome(document)
    require(outcome.get("confirmation_status") == status, f"expected {status}, got {outcome.get('confirmation_status')}")
    return outcome


def require_blocker(outcome: Mapping[str, Any], fragment: str) -> str:
    blockers = outcome.get("blockers")
    require(isinstance(blockers, list), "outcome blockers are missing")
    match = next((str(item) for item in blockers if fragment in str(item)), None)
    require(match is not None, f"expected blocker containing {fragment!r}; observed {blockers!r}")
    return str(match)


def case_snapshot_confirmed(root: pathlib.Path) -> Dict[str, Any]:
    source, witness, command = prepare(root, "cmd-action-outcome-confirmed")
    document = build_action_outcomes(root, witness, health_for(witness))
    outcome = assert_status(document, CONFIRMATION_CONFIRMED)
    require(outcome.get("classification") is None, "confirmed outcome was classified as a failure")
    require(outcome.get("blockers") == [], "confirmed outcome contains blockers")
    require(outcome.get("command_location") == "confirming", "certificate did not originate in confirming/")
    require(
        outcome.get("executor_status") == "executed_pending_snapshot",
        "certificate lost the pre-confirmation executor status",
    )
    require(outcome.get("command_intent_hash") == command_intent_hash(command), "intent certificate changed")
    require(outcome.get("source", {}).get("state_hash") == canonical_state_hash(source), "source hash is wrong")
    require(outcome.get("witness", {}).get("state_hash") == canonical_state_hash(witness), "witness hash is wrong")
    require(
        int(outcome.get("witness", {}).get("seq") or 0) > int(command["local_effect_ack_seq"]),
        "witness sequence did not advance beyond local_effect_observed ACK",
    )
    require(
        command["accepted_ack_seq"] < command["executed_ack_seq"] < command["local_effect_ack_seq"],
        "fixture ACK sequence is not strictly ordered",
    )
    require(document.get("counts", {}).get(CONFIRMATION_CONFIRMED) == 1, "confirmed count is wrong")
    write_json_atomic(root / "action-outcomes.json", document)
    loaded, blockers = read_action_outcome(root, command["id"])
    require(not blockers and loaded is not None, "valid persisted attestation could not be read")
    require(loaded.get("confirmation_status") == CONFIRMATION_CONFIRMED, "persisted attestation changed status")
    return {
        "command_id": command["id"],
        "attempt_id": command["attempt_id"],
        "command_location": outcome["command_location"],
        "source_seq": outcome["source"]["seq"],
        "accepted_ack_seq": command["accepted_ack_seq"],
        "executed_ack_seq": command["executed_ack_seq"],
        "local_effect_ack_seq": command["local_effect_ack_seq"],
        "witness_seq": outcome["witness"]["seq"],
        "status": outcome["confirmation_status"],
    }


def case_waits_without_later_snapshot(root: pathlib.Path) -> Dict[str, Any]:
    source, _, command = prepare(root, "cmd-action-outcome-waiting")
    document = build_action_outcomes(root, source, health_for(source))
    outcome = assert_status(document, CONFIRMATION_WAITING)
    blocker = require_blocker(outcome, "witness_not_newer_than_source")
    require(outcome.get("classification") is None, "waiting outcome was misclassified as terminal failure")
    require(
        document.get("counts", {}).get(CONFIRMATION_CONFIRMED) == 0,
        "confirming command auto-confirmed without a later witness",
    )
    return {"command_id": command["id"], "status": outcome["confirmation_status"], "blocker": blocker}


def case_requires_ordered_archived_acks(root: pathlib.Path) -> Dict[str, Any]:
    missing_root = root / "missing-local-effect-ack"
    source, witness, command = fixture("cmd-action-outcome-missing-ack")
    write_session_history(
        missing_root,
        source,
        command,
        ack_statuses=("accepted", "executed"),
    )
    write_command(missing_root, command)
    missing_outcome = assert_status(
        build_action_outcomes(missing_root, witness, health_for(witness)),
        CONFIRMATION_WAITING,
    )
    missing_blocker = require_blocker(
        missing_outcome,
        "local_effect_observed_ack_not_archived",
    )

    unordered_root = root / "unordered-acks"
    unordered_source, unordered_witness, unordered = fixture("cmd-action-outcome-unordered-ack")
    unordered["executed_ack_seq"] = unordered["accepted_ack_seq"]
    write_session_history(unordered_root, unordered_source, unordered)
    write_command(unordered_root, unordered)
    unordered_outcome = assert_status(
        build_action_outcomes(unordered_root, unordered_witness, health_for(unordered_witness)),
        CONFIRMATION_REJECTED,
    )
    unordered_blocker = require_blocker(unordered_outcome, "ack_sequence_order_invalid")

    same_seq_root = root / "witness-on-local-effect-ack"
    _, proper_witness, _ = prepare(same_seq_root, "cmd-action-outcome-witness-on-ack")
    same_seq_witness = copy.deepcopy(proper_witness)
    same_seq_witness["seq"] = LOCAL_EFFECT_ACK_SEQ
    same_seq_witness["snapshot_id"] = f"{same_seq_witness['session_id']}:{LOCAL_EFFECT_ACK_SEQ}"
    same_seq_outcome = assert_status(
        build_action_outcomes(same_seq_root, same_seq_witness, health_for(same_seq_witness)),
        CONFIRMATION_WAITING,
    )
    same_seq_blocker = require_blocker(
        same_seq_outcome,
        "witness_not_after_local_effect_ack",
    )
    return {
        "missing_ack_blocker": missing_blocker,
        "unordered_ack_blocker": unordered_blocker,
        "same_sequence_witness_blocker": same_seq_blocker,
    }


def case_rejects_unapproved_and_forged_done(root: pathlib.Path) -> Dict[str, Any]:
    source, witness, command = fixture("cmd-action-outcome-unapproved")
    command.pop("approval_receipt", None)
    command.pop("approved_by", None)
    command.pop("approved_at", None)
    write_session_history(root, source, command)
    write_command(root, command)
    outcome = assert_status(build_action_outcomes(root, witness, health_for(witness)), CONFIRMATION_REJECTED)
    approval_blocker = require_blocker(outcome, "approval")

    forged_root = root / "intent-tamper"
    forged_source, forged_witness, forged = fixture("cmd-action-outcome-forged")
    forged["args"]["paused"] = False
    write_session_history(forged_root, forged_source, forged)
    write_command(forged_root, forged)
    forged_outcome = assert_status(
        build_action_outcomes(forged_root, forged_witness, health_for(forged_witness)),
        CONFIRMATION_REJECTED,
    )
    intent_blocker = require_blocker(forged_outcome, "intent")

    duplicate_root = root / "duplicate-location"
    dup_source, dup_witness, duplicate = prepare(duplicate_root, "cmd-action-outcome-duplicate")
    write_command(duplicate_root, duplicate, "failed")
    duplicate_outcome = assert_status(
        build_action_outcomes(duplicate_root, dup_witness, health_for(dup_witness)),
        CONFIRMATION_REJECTED,
    )
    duplicate_blocker = require_blocker(duplicate_outcome, "command_location_ambiguous")
    return {
        "unapproved_blocker": approval_blocker,
        "forged_intent_blocker": intent_blocker,
        "duplicate_blocker": duplicate_blocker,
    }


def case_rejects_source_hash_tamper(root: pathlib.Path) -> Dict[str, Any]:
    source, witness, command = fixture("cmd-action-outcome-source-hash")
    command["state_hash"] = "sha256:" + "f" * 64
    write_session_history(root, source, command)
    write_command(root, command)
    outcome = assert_status(build_action_outcomes(root, witness, health_for(witness)), CONFIRMATION_REJECTED)
    blocker = require_blocker(outcome, "source_state_hash_mismatch")
    return {"status": outcome["confirmation_status"], "blocker": blocker}


def case_rejects_wrong_or_old_witness(root: pathlib.Path) -> Dict[str, Any]:
    source, witness, command = prepare(root, "cmd-action-outcome-identity")

    old = assert_status(build_action_outcomes(root, source, health_for(source)), CONFIRMATION_WAITING)
    old_blocker = require_blocker(old, "witness_not_newer_than_source")

    wrong_session = copy.deepcopy(witness)
    wrong_session["session_id"] = "session-action-outcome-wrong"
    wrong_session["snapshot_id"] = f"{wrong_session['session_id']}:{wrong_session['seq']}"
    wrong_session_document = build_action_outcomes(root, wrong_session, health_for(wrong_session))
    validate_action_outcomes(wrong_session_document)
    require(wrong_session_document.get("outcomes") == [], "old-session command was attested by a different session")

    wrong_save = copy.deepcopy(witness)
    wrong_save["save_id_hash"] = OTHER_SAVE_HASH
    wrong_save_outcome = assert_status(
        build_action_outcomes(root, wrong_save, health_for(wrong_save)),
        CONFIRMATION_REJECTED,
    )
    save_blocker = require_blocker(wrong_save_outcome, "witness_save_id_hash_mismatch")

    wrong_world = copy.deepcopy(witness)
    wrong_world["world_id"] = 1
    wrong_world["observation_scope"] = {
        "active_world_id": 1,
        "populated_world_ids": [1],
        "covered_world_ids": [1],
        "complete_for_populated_worlds": True,
    }
    wrong_world_outcome = assert_status(
        build_action_outcomes(root, wrong_world, health_for(wrong_world)),
        CONFIRMATION_REJECTED,
    )
    world_blocker = require_blocker(wrong_world_outcome, "witness_world_id_mismatch")
    return {
        "command_id": command["id"],
        "old_witness_blocker": old_blocker,
        "wrong_session_outcome_count": 0,
        "wrong_save_blocker": save_blocker,
        "wrong_world_blocker": world_blocker,
    }


def case_blocks_stale_and_quality_tamper(root: pathlib.Path) -> Dict[str, Any]:
    source, witness, _ = prepare(root, "cmd-action-outcome-trust")
    stale = copy.deepcopy(witness)
    stale["ts"] = iso(dt.datetime.now(dt.timezone.utc).astimezone() - dt.timedelta(minutes=2))
    stale_outcome = assert_status(
        build_action_outcomes(
            root,
            stale,
            health_for(stale, status="stale", trust_ready=False, blockers=["snapshot_stale"]),
        ),
        CONFIRMATION_WAITING,
    )
    stale_blocker = require_blocker(stale_outcome, "witness_snapshot_stale")

    quality = copy.deepcopy(witness)
    quality["quality"]["game.paused"]["contract_id"] = "oni.qc.forged.v1"
    quality_outcome = assert_status(
        build_action_outcomes(root, quality, health_for(quality)),
        CONFIRMATION_REJECTED,
    )
    quality_blocker = require_blocker(quality_outcome, "quality_contract_id_mismatch")

    source_tamper = health_for(witness)
    source_tamper["source"]["state_hash"] = "sha256:" + "0" * 64
    source_outcome = assert_status(
        build_action_outcomes(root, witness, source_tamper),
        CONFIRMATION_WAITING,
    )
    source_blocker = require_blocker(source_outcome, "health_witness_source_mismatch")
    require(source["snapshot_id"] != witness["snapshot_id"], "fixture does not contain an independent later snapshot")
    return {
        "stale_blocker": stale_blocker,
        "quality_blocker": quality_blocker,
        "health_source_blocker": source_blocker,
    }


def expect_validation_error(document: Mapping[str, Any], message: str) -> str:
    try:
        validate_action_outcomes(document)
    except ActionOutcomeError as exc:
        return str(exc)
    raise AssertionError(message)


def case_detects_attestation_and_output_hash_tamper(root: pathlib.Path) -> Dict[str, Any]:
    _, witness, command = prepare(root, "cmd-action-outcome-hash")
    valid = build_action_outcomes(root, witness, health_for(witness))
    assert_status(valid, CONFIRMATION_CONFIRMED)

    attestation_tamper = copy.deepcopy(valid)
    attestation_tamper["outcomes"][0]["witness"]["seq"] += 1
    attestation_tamper["outcomes_hash"] = document_hash(attestation_tamper, "outcomes_hash")
    attestation_error = expect_validation_error(
        attestation_tamper,
        "tampered attestation passed after the attacker repaired only the outer hash",
    )
    require("attestation hash" in attestation_error, "attestation tamper failed for the wrong reason")

    output_tamper = copy.deepcopy(valid)
    output_tamper["counts"][CONFIRMATION_CONFIRMED] = 0
    output_error = expect_validation_error(output_tamper, "tampered output hash was accepted")
    require("outcomes hash" in output_error, "output tamper failed for the wrong reason")

    write_json_atomic(root / "action-outcomes.json", attestation_tamper)
    loaded, blockers = read_action_outcome(root, command["id"])
    require(loaded is None and blockers == ["action_outcomes_invalid"], "reader accepted a tampered certificate")
    return {"attestation_error": attestation_error, "output_error": output_error, "reader_blockers": blockers}


def case_ignores_nonmutating_done(root: pathlib.Path) -> Dict[str, Any]:
    _source, witness, command = fixture("cmd-action-outcome-focus-note")
    command.update(
        {
            "schema": "oni.command.v2",
            "status": "verified",
            "action": "focus_note",
            "args": {"tag": "offline-attestor-check", "message": "no game mutation"},
            "preconditions": [],
            "expected_effects": [],
        }
    )
    command.pop("approval_receipt", None)
    command.pop("confirmation", None)
    write_command(root, command, "done")
    document = build_action_outcomes(root, witness, health_for(witness))
    validate_action_outcomes(document)
    require(document.get("outcomes") == [], "non-mutating done entered snapshot attestation")
    require(
        all(int(value or 0) == 0 for value in (document.get("counts") or {}).values()),
        "non-mutating done changed mutation confirmation counts",
    )
    return {"command_id": command["id"], "action": command["action"], "outcome_count": 0}


def run_case(
    name: str,
    callback: Callable[[pathlib.Path], Dict[str, Any]],
    root: pathlib.Path,
) -> Dict[str, Any]:
    case_root = root / name
    case_root.mkdir(parents=True, exist_ok=True)
    try:
        return {"name": name, "status": "passed", "details": callback(case_root)}
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "classification": "behavior_fail",
            "error": str(exc),
        }


def run(report_path: pathlib.Path) -> int:
    cases_to_run: list[tuple[str, Callable[[pathlib.Path], Dict[str, Any]]]] = [
        ("snapshot_confirmed", case_snapshot_confirmed),
        ("waits_without_later_snapshot", case_waits_without_later_snapshot),
        ("requires_ordered_archived_acks", case_requires_ordered_archived_acks),
        ("rejects_unapproved_and_forged_done", case_rejects_unapproved_and_forged_done),
        ("rejects_source_hash_tamper", case_rejects_source_hash_tamper),
        ("rejects_wrong_or_old_witness", case_rejects_wrong_or_old_witness),
        ("blocks_stale_and_quality_tamper", case_blocks_stale_and_quality_tamper),
        ("detects_attestation_and_output_hash_tamper", case_detects_attestation_and_output_hash_tamper),
        ("ignores_nonmutating_done", case_ignores_nonmutating_done),
    ]
    with tempfile.TemporaryDirectory(prefix="oni-action-outcomes-") as temporary:
        temporary_root = pathlib.Path(temporary)
        cases = [run_case(name, callback, temporary_root) for name, callback in cases_to_run]

    passed = all(item["status"] == "passed" for item in cases)
    report = {
        "schema": "oni.action-outcomes-verification.v1",
        "generated_at": iso(dt.datetime.now(dt.timezone.utc).astimezone()),
        "mode": "offline_adversarial",
        "game_launched": False,
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "cases": cases,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for item in cases:
        detail = item.get("details") if item["status"] == "passed" else item.get("error")
        print(f"[{item['status'].upper()}] {item['name']}: {json.dumps(detail, ensure_ascii=False, sort_keys=True)}")
    print(f"Report: {report_path}")
    return 0 if passed else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=pathlib.Path,
        default=PROJECT_ROOT / "reports" / "action-outcomes-verification.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run(args.report.expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())
