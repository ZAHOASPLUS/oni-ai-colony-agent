#!/usr/bin/env python3
"""Exercise the save-lineage reservation, activation, checkpoint, and tamper gates."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.action_outcomes import build_action_outcomes, commit_confirmed_outcomes
from agent.state_quality import canonical_state_hash
from scripts.verify_action_outcomes import (
    direct_state,
    fixture as action_outcome_fixture,
    health_for,
    make_approval_receipt,
    make_command_ack,
    make_snapshot,
)
from scripts.verify_priority_pair_lineage import run_fixture as run_priority_pair_fixture

from manage_save_lineage import (
    LineageError,
    activate,
    checkpoint,
    checkpoint_hash as lineage_checkpoint_hash,
    load_json,
    path_id,
    rehash_checkpoints,
    reserve,
    sha256_file,
    verify_lineage,
    write_json_atomic,
)


MOD_VERSION = "0.7.0"


def snapshot(session_id: str, seq: int, save_hash: str) -> dict:
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": f"2026-07-11T08:00:{seq:02d}+08:00",
        "session_id": session_id,
        "seq": seq,
        "snapshot_id": f"{session_id}:{seq}",
        "game_build": "cl-fixture",
        "mod_version": MOD_VERSION,
        "save_id_hash": save_hash,
        "save_dirty": False,
        "world_id": 0,
        "bridge": {"stale": False},
    }


def save_event(session_id: str, seq: int, before_hash: str, after_hash: str, save_path_id: str) -> dict:
    return {
        "schema": "oni.event.v2",
        "event": "save_write_completed",
        "ts": f"2026-07-11T08:01:{seq:02d}+08:00",
        "session_id": session_id,
        "seq": seq,
        "game_build": "cl-fixture",
        "mod_version": MOD_VERSION,
        "save_id_hash": after_hash,
        "save_dirty": False,
        "world_id": 0,
        "success": True,
        "is_auto_save": False,
        "update_save_pointer": True,
        "file_rewrite_observed": True,
        "save_file_readable": True,
        "failure_reason": None,
        "requested_save_path_id": save_path_id,
        "result_save_path_id": save_path_id,
        "active_save_path_id": save_path_id,
        "before_save_id_hash": before_hash,
        "written_save_id_hash": after_hash,
        "active_save_id_hash": after_hash,
    }


def append_records(path: pathlib.Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def run_fixture() -> dict:
    with tempfile.TemporaryDirectory(prefix="oni-save-lineage-") as temporary:
        root = pathlib.Path(temporary)
        bridge = root / "bridge"
        bridge.mkdir()
        save_path = root / "new-validation-save.sav"
        source_path = root / "source.sav"
        source_path.write_bytes(b"source-save")
        source_hash = sha256_file(source_path)
        source_snapshot = snapshot("session-source", 5, source_hash)
        write_json_atomic(bridge / "latest_state.json", source_snapshot)
        write_json_atomic(
            bridge / "action-policy.json",
            {
                "schema": "oni.action-policy.v1",
                "actions_enabled": False,
                "allowed_save_hashes": [],
                "allowed_actions": ["noop", "focus_note"],
                "command_ttl_seconds": 30,
                "max_pending_commands": 20,
            },
        )
        append_records(bridge / "state.jsonl", source_snapshot)

        reserved = reserve(
            argparse.Namespace(
                run_id="fixture-v27",
                bridge_dir=bridge,
                save_path=save_path,
                mod_version=MOD_VERSION,
                source_kind="save_as_copy",
                allow_external_save=True,
            )
        )
        manifest_path = pathlib.Path(reserved["manifest"])
        duplicate_reservation_blocked = False
        try:
            reserve(
                argparse.Namespace(
                    run_id="fixture-v27-duplicate",
                    bridge_dir=bridge,
                    save_path=source_path,
                    mod_version=MOD_VERSION,
                    source_kind="save_as_copy",
                    allow_external_save=True,
                )
            )
        except LineageError:
            duplicate_reservation_blocked = True

        save_path.write_bytes(b"new-save-initial")
        initial_hash = sha256_file(save_path)
        activation_event = save_event("session-source", 6, source_hash, initial_hash, path_id(save_path))
        fixture_now = dt.datetime.now(dt.timezone.utc).astimezone()
        initial_reload = make_snapshot(
            seq=10,
            snapshot_seq=1,
            paused=False,
            timestamp=fixture_now - dt.timedelta(seconds=9),
            session_id="session-initial",
            save_hash=initial_hash,
        )
        initial_reload["mod_version"] = MOD_VERSION
        append_records(bridge / "state.jsonl", activation_event, initial_reload)
        write_json_atomic(bridge / "latest_state.json", initial_reload)
        activate(argparse.Namespace(manifest=manifest_path, bridge_dir=bridge))

        command_id = "cmd-runtime-fixture-confirmed"
        _, witness, command = action_outcome_fixture(command_id)
        witness = make_snapshot(
            seq=14,
            snapshot_seq=2,
            paused=True,
            timestamp=fixture_now - dt.timedelta(seconds=1),
            session_id="session-initial",
            save_hash=initial_hash,
        )
        witness["mod_version"] = MOD_VERSION
        command.update(
            {
                "session_id": initial_reload["session_id"],
                "save_id_hash": initial_reload["save_id_hash"],
                "world_id": initial_reload["world_id"],
                "source_snapshot_id": initial_reload["snapshot_id"],
                "state_hash": canonical_state_hash(initial_reload),
                "result": {
                    "before_state": direct_state(initial_reload),
                    "after_state": direct_state(witness),
                },
            }
        )
        command["source"]["state_seq"] = initial_reload["seq"]
        command["source"]["state_ts"] = initial_reload["ts"]
        command["approval_receipt"] = make_approval_receipt(command)
        command_path = bridge / "commands" / "confirming" / f"{command_id}.json"
        write_json_atomic(command_path, command)
        command_records = [
            make_command_ack(initial_reload, command, stage)
            for stage in ("accepted", "executed", "local_effect_observed")
        ]
        append_records(bridge / "state.jsonl", *command_records, witness)
        append_records(
            bridge / "sessions" / "session-initial" / "state.jsonl",
            initial_reload,
            *command_records,
            witness,
        )
        outcomes = build_action_outcomes(bridge, witness, health_for(witness))
        root_certificate = bridge / "action-outcomes.json"
        session_certificate = bridge / "sessions" / "session-initial" / "action-outcomes.json"
        write_json_atomic(root_certificate, outcomes)
        write_json_atomic(session_certificate, outcomes)
        committed = commit_confirmed_outcomes(
            bridge,
            outcomes,
            publication_paths=(root_certificate, session_certificate),
        )
        if committed != [command_id]:
            raise RuntimeError(f"confirmation fixture did not commit: {committed!r}")
        save_path.write_bytes(b"new-save-after-research")
        checkpoint_hash = sha256_file(save_path)
        checkpoint_event = save_event("session-initial", 15, initial_hash, checkpoint_hash, path_id(save_path))
        checkpoint_reload = snapshot("session-after-research", 1, checkpoint_hash)
        append_records(bridge / "state.jsonl", checkpoint_event, checkpoint_reload)
        write_json_atomic(bridge / "latest_state.json", checkpoint_reload)
        mutable_artifact = root / "runtime-marker.passed"
        mutable_artifact.write_text("original marker\n", encoding="utf-8")
        checkpoint(
            argparse.Namespace(
                manifest=manifest_path,
                bridge_dir=bridge,
                step_id="research-set",
                scenario="set_research persists after save and reload",
                outcome="confirmed",
                expected_mutation="persisted_change",
                command_id=command_id,
                evidence=[str(mutable_artifact)],
            )
        )

        mutable_artifact.write_text("rewritten marker\n", encoding="utf-8")

        report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
        )
        manifest = load_json(manifest_path)
        frozen_artifact = pathlib.Path(manifest["checkpoints"][0]["artifacts"][0]["path"])
        artifact_frozen = (
            frozen_artifact.is_file()
            and frozen_artifact.read_text(encoding="utf-8") == "original marker\n"
            and frozen_artifact != mutable_artifact
        )

        legacy_command_id = "cmd-runtime-fixture-legacy-verified"
        legacy_command_path = bridge / "commands" / "done" / f"{legacy_command_id}.json"
        legacy_before = manifest["checkpoints"][0]["before"]
        write_json_atomic(
            legacy_command_path,
            {
                "schema": "oni.command.v3",
                "id": legacy_command_id,
                "action": "set_research",
                "session_id": legacy_before["session_id"],
                "save_id_hash": legacy_before["save_id_hash"],
                "world_id": legacy_before["world_id"],
                "status": "verified",
            },
        )
        legacy_manifest = copy.deepcopy(manifest)
        legacy_manifest["checkpoints"][0]["outcome"] = "verified"
        legacy_manifest["checkpoints"][0]["command"] = {
            "id": legacy_command_id,
            "action": "set_research",
            "directory": "done",
            "status": "verified",
            "classification": None,
            "ack_status": "verified",
            "file": f"commands/done/{legacy_command_id}.json",
            "file_sha256": sha256_file(legacy_command_path),
        }
        rehash_checkpoints(legacy_manifest)
        write_json_atomic(manifest_path, legacy_manifest)
        legacy_offline_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )
        legacy_live_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=True,
        )
        write_json_atomic(manifest_path, manifest)

        wrong_order_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("observer-start",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        missing_artifact = copy.deepcopy(manifest)
        missing_artifact["checkpoints"][0]["artifacts"] = []
        rehash_checkpoints(missing_artifact)
        write_json_atomic(manifest_path, missing_artifact)
        missing_artifact_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        bound_policy = copy.deepcopy(manifest)
        bound_policy["checkpoints"][0]["policy_gate"]["allowed_save_hash_count"] = 1
        rehash_checkpoints(bound_policy)
        write_json_atomic(manifest_path, bound_policy)
        bound_policy_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        unrotated = copy.deepcopy(manifest)
        before_hash = unrotated["checkpoints"][0]["before"]["save_id_hash"]
        unrotated["checkpoints"][0]["save"]["active_save_id_hash"] = before_hash
        unrotated["checkpoints"][0]["save"]["written_save_id_hash"] = before_hash
        unrotated["checkpoints"][0]["reload"]["save_id_hash"] = before_hash
        unrotated["latest"]["save_id_hash"] = before_hash
        rehash_checkpoints(unrotated)
        write_json_atomic(manifest_path, unrotated)
        unrotated_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        origin_mismatch = copy.deepcopy(manifest)
        origin_mismatch["reservation_source"]["session_id"] = "session-origin-attacker"
        write_json_atomic(manifest_path, origin_mismatch)
        origin_mismatch_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        activation_before_hash_mismatch = copy.deepcopy(manifest)
        activation_before_hash_mismatch["initial"]["save"]["before_save_id_hash"] = (
            "sha256:" + "a" * 64
        )
        activation_before_hash_mismatch["initial"]["checkpoint_hash"] = lineage_checkpoint_hash(
            activation_before_hash_mismatch["initial"]
        )
        rehash_checkpoints(activation_before_hash_mismatch)
        write_json_atomic(manifest_path, activation_before_hash_mismatch)
        activation_before_hash_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        activation_written_hash_mismatch = copy.deepcopy(manifest)
        activation_written_hash_mismatch["initial"]["save"]["written_save_id_hash"] = (
            "sha256:" + "b" * 64
        )
        activation_written_hash_mismatch["initial"]["checkpoint_hash"] = lineage_checkpoint_hash(
            activation_written_hash_mismatch["initial"]
        )
        rehash_checkpoints(activation_written_hash_mismatch)
        write_json_atomic(manifest_path, activation_written_hash_mismatch)
        activation_written_hash_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        checkpoint_session_mismatch = copy.deepcopy(manifest)
        checkpoint_session_mismatch["checkpoints"][0]["save"]["session_id"] = (
            "session-checkpoint-attacker"
        )
        rehash_checkpoints(checkpoint_session_mismatch)
        write_json_atomic(manifest_path, checkpoint_session_mismatch)
        checkpoint_session_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        checkpoint_written_hash_mismatch = copy.deepcopy(manifest)
        checkpoint_written_hash_mismatch["checkpoints"][0]["save"][
            "written_save_id_hash"
        ] = "sha256:" + "c" * 64
        rehash_checkpoints(checkpoint_written_hash_mismatch)
        write_json_atomic(manifest_path, checkpoint_written_hash_mismatch)
        checkpoint_written_hash_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
            verify_live=False,
        )

        tampered = copy.deepcopy(manifest)
        tampered["checkpoints"][0]["scenario"] = "tampered"
        write_json_atomic(manifest_path, tampered)
        tamper_report = verify_lineage(
            manifest_path,
            bridge,
            expected_mod_version=MOD_VERSION,
            required_steps=("research-set",),
            required_step_order=("research-set",),
            minimum_checkpoints=1,
        )
        write_json_atomic(manifest_path, manifest)
        priority_pair = run_priority_pair_fixture()
        return {
            "status": "passed"
            if report["status"] == "passed"
            and tamper_report["status"] != "passed"
            and duplicate_reservation_blocked
            and artifact_frozen
            and wrong_order_report["status"] != "passed"
            and missing_artifact_report["status"] != "passed"
            and bound_policy_report["status"] != "passed"
            and unrotated_report["status"] != "passed"
            and origin_mismatch_report["status"] != "passed"
            and activation_before_hash_report["status"] != "passed"
            and activation_written_hash_report["status"] != "passed"
            and checkpoint_session_report["status"] != "passed"
            and checkpoint_written_hash_report["status"] != "passed"
            and legacy_offline_report["status"] == "legacy_read_only"
            and legacy_live_report["status"] == "incomplete_or_failed"
            and priority_pair["status"] == "passed"
            and priority_pair["publication_write_failure_blocked"] is True
            and priority_pair["missing_frame_commit_blocked"] is True
            and priority_pair["conflicting_bundle_commit_blocked"] is True
            and priority_pair["same_round_prune_survived"] is True
            and priority_pair["command_pipeline_gate_bound"] is True
            and priority_pair["relevant_failed_outcome_unknown_blocker"]
            == "command_pipeline_mutation_not_ready"
            else "failed",
            "valid_lineage": report["status"],
            "tamper_rejected": tamper_report["status"] != "passed",
            "existing_save_reservation_rejected": duplicate_reservation_blocked,
            "checkpoint_artifact_frozen": artifact_frozen,
            "wrong_order_rejected": wrong_order_report["status"] != "passed",
            "missing_artifact_rejected": missing_artifact_report["status"] != "passed",
            "bound_policy_rejected": bound_policy_report["status"] != "passed",
            "unrotated_save_rejected": unrotated_report["status"] != "passed",
            "reservation_source_activation_source_mismatch_rejected": (
                origin_mismatch_report["status"] != "passed"
            ),
            "activation_before_hash_mismatch_rejected": (
                activation_before_hash_report["status"] != "passed"
            ),
            "activation_written_hash_mismatch_rejected": (
                activation_written_hash_report["status"] != "passed"
            ),
            "checkpoint_session_mismatch_rejected": (
                checkpoint_session_report["status"] != "passed"
            ),
            "checkpoint_written_hash_mismatch_rejected": (
                checkpoint_written_hash_report["status"] != "passed"
            ),
            "legacy_offline_classification": legacy_offline_report["status"],
            "legacy_live_rejected": legacy_live_report["status"] == "incomplete_or_failed",
            "priority_pair_lineage": priority_pair["status"],
            "priority_pair_adversarial_rejection_count": priority_pair[
                "adversarial_rejection_count"
            ],
            "priority_pair_pre_done_bundle": priority_pair["automatic_pre_done_bundle"],
            "priority_pair_publication_failure_blocked": priority_pair[
                "publication_write_failure_blocked"
            ],
            "priority_pair_missing_frame_blocked": priority_pair["missing_frame_commit_blocked"],
            "priority_pair_conflicting_bundle_blocked": priority_pair[
                "conflicting_bundle_commit_blocked"
            ],
            "priority_pair_same_round_prune_survived": priority_pair["same_round_prune_survived"],
            "priority_pair_command_pipeline_gate_bound": priority_pair[
                "command_pipeline_gate_bound"
            ],
            "priority_pair_outcome_unknown_blocker": priority_pair[
                "relevant_failed_outcome_unknown_blocker"
            ],
        }


def main() -> int:
    result = run_fixture()
    report_path = pathlib.Path(__file__).resolve().parents[1] / "reports" / "save-lineage-verification.json"
    write_json_atomic(report_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"Report: {report_path}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
