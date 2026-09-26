#!/usr/bin/env python3
"""TestRoot-only adversarial verification for normal-shutdown evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import py_compile
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "shutdown-evidence-verification.json"
CORE = ROOT / "scripts" / "create_shutdown_evidence.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.canonical_hash import canonical_hash
from agent.state_quality import canonical_state_hash
from scripts import create_shutdown_evidence as shutdown
from scripts import create_startup_consumption_evidence as startup
from scripts import verify_startup_consumption_evidence as startup_verifier


ASSERTION_COUNT = 0


class VerificationFailure(RuntimeError):
    pass


def assert_true(condition: bool, message: str) -> None:
    global ASSERTION_COUNT
    ASSERTION_COUNT += 1
    if not condition:
        raise VerificationFailure(message)


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_jsonl(path: pathlib.Path, records: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
            for record in records
        ),
        encoding="utf-8",
        newline="\n",
    )


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def rehash_lineage_chain(manifest: dict[str, Any]) -> None:
    initial = manifest["initial"]
    initial["checkpoint_hash"] = shutdown.lineage_contract.checkpoint_hash(initial)
    previous_hash = initial["checkpoint_hash"]
    for checkpoint in manifest["checkpoints"]:
        checkpoint["previous_checkpoint_hash"] = previous_hash
        checkpoint["checkpoint_hash"] = shutdown.lineage_contract.checkpoint_hash(checkpoint)
        previous_hash = checkpoint["checkpoint_hash"]
    manifest["latest_checkpoint_hash"] = previous_hash


def startup_capture(payload: bytes, label: str) -> startup.Capture:
    return startup.Capture(
        payload,
        startup.sha256_bytes(payload),
        len(payload),
        canonical_hash({"fixture_source": label}),
    )


def startup_common_fields(
    binding: Mapping[str, Any],
    binding_hash: str,
    process_id: int,
    session_id: str,
    *,
    world_id: int,
    save_id_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "game_build": binding["expected_game_build"],
        "mod_version": binding["expected_mod_version"],
        "save_id_hash": save_id_hash or binding["requested_loader_save_sha256"],
        "save_dirty": False,
        "world_id": world_id,
        "validation_run_id": binding["validation_run_id"],
        "startup_binding_hash": binding_hash,
        "startup_intent_hash": binding["startup_intent_hash"],
        "runtime_config_evidence_sha256": binding["runtime_config_evidence_sha256"],
        "validation_process_id": process_id,
        "startup_binding_status": "verified",
        "requested_save_path_id": binding["requested_loader_path_id_upper"],
        "active_save_path_id": binding["requested_loader_path_id_upper"],
    }


def make_startup_v2_v5_material(
    *,
    fixture_name: str,
    validation_run_id: str,
    loader_path_id: str,
    loader_save_sha256: str,
    loader_save_length: int,
    game_build: str,
    mod_version: str,
    expected_probe_dll_sha256: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    bytes,
    str,
    dict[str, Any],
    bytes,
    str,
]:
    environment_manifest_sha256 = canonical_hash({"environment": fixture_name})
    result_record = startup_verifier.make_runtime_bootstrap_result(
        run_id=validation_run_id,
        environment_manifest_sha256=environment_manifest_sha256,
        expected_mod_version=mod_version,
        expected_probe_dll_sha256=expected_probe_dll_sha256,
    )
    startup_intent: dict[str, Any] = {
        "schema": startup.INTENT_SCHEMA,
        "validation_run_id": validation_run_id,
        "startup_nonce": "startup-0123456789abcdef0123456789abcdef",
        "environment_manifest_sha256": environment_manifest_sha256,
        "requested_loader_path_id_lower": canonical_hash(
            {"loader_lower": fixture_name}
        ),
        "requested_loader_path_id_upper": loader_path_id,
        "requested_loader_save_sha256": loader_save_sha256,
        "requested_loader_save_length": loader_save_length,
        "expected_game_build": game_build,
        "expected_mod_version": mod_version,
        "expected_probe_dll_sha256": expected_probe_dll_sha256,
        "runtime_bootstrap_result_path_id_lower": result_record["path_id_lower"],
        "runtime_bootstrap_result_sha256": result_record[
            "external_expected_sha256"
        ],
        "runtime_bootstrap_result_hash": result_record["result_hash"],
        "runtime_bootstrap_transaction_id": result_record["transaction_id"],
        "installed_probe_tree_content_hash": result_record[
            "probe_tree_content_hash"
        ],
        "auto_resume_semantics": "one_shot_nonempty_exact_loader",
    }
    startup_intent["document_hash"] = startup.json_document_hash(startup_intent)
    runtime_evidence: dict[str, Any] = {
        "schema": "oni.live-validation-runtime-config.v5",
        "run_id": validation_run_id,
        "mode": "apply",
        "configured_at": "2026-08-31T01:59:59+00:00",
        "transaction_id": f"config-{fixture_name}",
        "environment_manifest_sha256": environment_manifest_sha256,
        "bindings": {},
        "runtime": {
            "bootstrap": {},
            "runtime_bootstrap_result": result_record,
            "release_dll": {},
            "installed_probe": {},
            "initial_disabled_policy": {},
            "mod_version": mod_version,
            "probe_static_id": "oni-ai-colony-agent",
            "dll_four_way_identical": True,
        },
        "configs": {},
        "loader_policy": {},
        "startup_intent": startup_intent,
        "mod_policy": {},
        "restoration": {},
    }
    runtime_evidence["document_hash"] = startup.json_document_hash(runtime_evidence)
    runtime_payload = startup.json_bytes(runtime_evidence)
    runtime_hash = startup.sha256_bytes(runtime_payload)
    startup_binding: dict[str, Any] = {
        "schema": startup.BINDING_SCHEMA,
        "validation_run_id": validation_run_id,
        "startup_nonce": startup_intent["startup_nonce"],
        "startup_intent_hash": startup_intent["document_hash"],
        "runtime_config_evidence_sha256": runtime_hash,
        "environment_manifest_sha256": environment_manifest_sha256,
        "expected_game_build": game_build,
        "expected_mod_version": mod_version,
        "expected_probe_dll_sha256": expected_probe_dll_sha256,
        "runtime_bootstrap_result_path_id_lower": result_record["path_id_lower"],
        "runtime_bootstrap_result_sha256": result_record[
            "external_expected_sha256"
        ],
        "runtime_bootstrap_result_hash": result_record["result_hash"],
        "runtime_bootstrap_transaction_id": result_record["transaction_id"],
        "installed_probe_tree_content_hash": result_record[
            "probe_tree_content_hash"
        ],
        "requested_loader_path_id_upper": loader_path_id,
        "requested_loader_save_sha256": loader_save_sha256,
        "requested_loader_save_length": loader_save_length,
        "created_at": runtime_evidence["configured_at"],
    }
    startup_binding["document_hash"] = startup.json_document_hash(startup_binding)
    startup_binding_payload = startup.json_bytes(startup_binding)
    startup_binding_hash = startup.sha256_bytes(startup_binding_payload)
    return (
        startup_intent,
        runtime_evidence,
        runtime_payload,
        runtime_hash,
        startup_binding,
        startup_binding_payload,
        startup_binding_hash,
    )


def _bundle(bridge: pathlib.Path, command_id: str, sequence: int) -> tuple[dict[str, Any], pathlib.Path]:
    command = {
        "schema": "oni.command.v3",
        "id": command_id,
        "status": "confirmed",
        "action": shutdown.ACTION,
        "args": {"duplicant_instance_id": 17, "chore_group": "Dig", "priority": sequence},
        "result": {"classification": "snapshot_confirmed_mutation_v1"},
    }
    bundle: dict[str, Any] = {
        "schema": shutdown.BUNDLE_SCHEMA,
        "command_id": command_id,
        "action": shutdown.ACTION,
        "command_intent_hash": canonical_hash({"id": command_id, "sequence": sequence}),
        "final_command_hash": canonical_hash(command),
        "final_command": command,
        "source_policy_hash": canonical_hash({"policy": command_id}),
        "source_policy": {"fixture": True},
        "approval_receipt_hash": canonical_hash({"receipt": command_id}),
        "source_frame": {"fixture": sequence},
        "bundle_hash": "",
    }
    bundle["bundle_hash"] = shutdown._hash_without(bundle, "bundle_hash")
    digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    path = bridge / "confirmed-command-evidence" / digest / "approval-bundle.json"
    write_json(path, bundle)
    return bundle, path


def _make_same_session_legacy_fixture(
    base: pathlib.Path,
    name: str,
    *,
    root_log: bool = False,
    startup_run_id: str | None = None,
) -> dict[str, Any]:
    case = base / name
    bridge = case / "ai-bridge"
    run_id = f"g0-{name}"
    certificate_run_id = startup_run_id or run_id
    lineage_path = bridge / "evidence" / "save-lineage" / run_id / "manifest.json"
    report_path = lineage_path.parent / "priority-pair-finalization.json"
    output_path = lineage_path.parent / "shutdown-evidence.json"
    session_id = f"session-{name}"
    save_id_hash = "sha256:" + hashlib.sha256(f"save-{name}".encode("utf-8")).hexdigest()
    world_id = 0
    loader_path = case / "fixture.sav"
    loader_path_text = str(loader_path)
    loader_path_id = startup.path_id_upper(loader_path_text)
    validation_process_id = 4242
    (
        startup_intent,
        runtime_evidence,
        runtime_payload,
        runtime_hash,
        startup_binding,
        startup_binding_payload,
        startup_binding_hash,
    ) = make_startup_v2_v5_material(
        fixture_name=name,
        validation_run_id=certificate_run_id,
        loader_path_id=loader_path_id,
        loader_save_sha256=save_id_hash,
        loader_save_length=987654,
        game_build="cl-fixture",
        mod_version="0.7.0",
        expected_probe_dll_sha256=canonical_hash({"probe": name}),
    )
    startup_fields = startup_common_fields(
        startup_binding,
        startup_binding_hash,
        validation_process_id,
        session_id,
        world_id=world_id,
    )
    snapshot_id = f"{session_id}:4"
    snapshot = {
        **startup_fields,
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "seq": 4,
        "snapshot_seq": 2,
        "snapshot_id": snapshot_id,
        "ts": "2026-08-31T02:00:01+00:00",
        "sample": {"interval_seconds": 5.0},
        "writer": {"dropped_snapshot_count": 0, "fault_count": 0},
    }
    state_hash = canonical_state_hash(snapshot)
    latest = {
        "session_id": session_id,
        "snapshot_id": snapshot_id,
        "save_id_hash": save_id_hash,
        "world_id": world_id,
        "mod_version": "0.7.0",
        "snapshot_ts": snapshot["ts"],
    }
    forward_step = "priority_forward"
    inverse_step = "priority_inverse"
    command_ids = [f"cmd-{name}-forward", f"cmd-{name}-inverse"]
    bundles = [_bundle(bridge, command_ids[0], 5), _bundle(bridge, command_ids[1], 3)]
    latest_checkpoint_hash = canonical_hash({"run_id": run_id, "checkpoint": 2})
    manifest = {
        "schema": shutdown.LINEAGE_SCHEMA,
        "test_run_id": run_id,
        "status": "active",
        "expected_mod_version": "0.7.0",
        "save_path": str(case / "fixture.sav"),
        "save_path_id": canonical_hash({"path": str(case / "fixture.sav")}),
        "latest": latest,
        "latest_checkpoint_hash": latest_checkpoint_hash,
        "checkpoints": [
            {"step_id": forward_step, "command": {"id": command_ids[0]}},
            {"step_id": inverse_step, "command": {"id": command_ids[1]}},
        ],
    }
    write_json(lineage_path, manifest)

    freezes: list[tuple[dict[str, Any], pathlib.Path]] = []
    for step_id, (bundle, bundle_path) in zip((forward_step, inverse_step), bundles):
        freeze: dict[str, Any] = {
            "schema": shutdown.FREEZE_SCHEMA,
            "step_id": step_id,
            "test_run_id": run_id,
            "manifest_binding": {"latest_checkpoint_hash": latest_checkpoint_hash},
            "approval_bundle": {
                "path": bundle_path.relative_to(bridge).as_posix(),
                "file_sha256": file_sha256(bundle_path),
                "bundle_hash": bundle["bundle_hash"],
                "document": bundle,
            },
            "confirmation_certificate": {"fixture": True},
            "confirmation_records": [{"event": "command_ack"}],
            "frozen_at": "2026-08-31T01:59:00+00:00",
            "freeze_hash": "",
        }
        freeze["freeze_hash"] = shutdown._hash_without(freeze, "freeze_hash")
        freeze_path = lineage_path.parent / "priority-command-freezes" / f"{step_id}.json"
        write_json(freeze_path, freeze)
        freezes.append((freeze, freeze_path))

    report_commands: list[dict[str, Any]] = []
    for step_id, command_id, (bundle, bundle_path), (freeze, freeze_path) in zip(
        (forward_step, inverse_step), command_ids, bundles, freezes
    ):
        report_commands.append(
            {
                "step_id": step_id,
                "id": command_id,
                "action": shutdown.ACTION,
                "automatic_approval_bundle": {
                    "path": bundle_path.relative_to(bridge).as_posix(),
                    "file_sha256": file_sha256(bundle_path),
                    "bundle_hash": bundle["bundle_hash"],
                },
                "lineage_freeze": {
                    "path": str(freeze_path),
                    "file_sha256": file_sha256(freeze_path),
                    "freeze_hash": freeze["freeze_hash"],
                },
            }
        )
    report: dict[str, Any] = {
        "schema": shutdown.PAIR_REPORT_SCHEMA,
        "status": "passed",
        "classification": None,
        "verification_mode": "offline_read_only",
        "fail_closed": True,
        "manifest": {
            "path": str(lineage_path),
            "file_sha256": file_sha256(lineage_path),
            "test_run_id": run_id,
            "latest_checkpoint_hash": latest_checkpoint_hash,
        },
        "pair": {
            "forward_step": forward_step,
            "inverse_step": inverse_step,
            "commands": report_commands,
        },
        "final_tip": {
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "save_id_hash": save_id_hash,
            "state_hash": state_hash,
        },
        "shutdown": {"actions_enabled": False, "queues": {"nonterminal": {}}},
        "report_hash": "",
    }
    report["report_hash"] = shutdown._hash_without(report, "report_hash")
    write_json(report_path, report)

    records: list[dict[str, Any]] = [
        {
            **startup_fields,
            "schema": "oni.event.v2",
            "event": "session_started",
            "reason": "save_loader_load_complete",
            "seq": 1,
            "ts": "2026-08-31T02:00:00+00:00",
        },
        {
            **startup_fields,
            "schema": "oni.event.v2",
            "event": "save_loaded",
            "reason": "save_loader_load_complete",
            "seq": 2,
            "ts": "2026-08-31T02:00:00.250000+00:00",
        },
        {
            **startup_fields,
            "schema": "oni.state.v2",
            "event": "state_snapshot",
            "seq": 3,
            "snapshot_seq": 1,
            "snapshot_id": f"{session_id}:3",
            "ts": "2026-08-31T02:00:00.500000+00:00",
            "sample": {"interval_seconds": 5.0},
            "writer": {"dropped_snapshot_count": 0, "fault_count": 0},
        },
        snapshot,
        {
            **startup_fields,
            "schema": "oni.event.v2",
            "event": "session_ended",
            "reason": "application_quit",
            "seq": 5,
            "ts": "2026-08-31T02:00:02+00:00",
        },
        {
            **startup_fields,
            "schema": "oni.event.v2",
            "event": "probe_shutdown",
            "seq": 6,
            "ts": "2026-08-31T02:00:02+00:00",
        },
    ]
    consumed_prefs_payload = (
        "strings:\r\n"
        "  InstalledLanguage: en\r\n"
        f"  SaveFilenameKey/: {loader_path_text}\r\n"
        "ints:\r\n"
        "  SESSION_ID: 1\r\n"
    ).encode("utf-8")
    player_log_payload = (
        "[INFO] boot\n"
        f"[INFO] Loading save file: {loader_path_text}\n"
        f"[INFO] Loaded [{loader_path_text}]\n"
        "[ONI AI Probe] STARTUP_BINDING_VERIFIED "
        f"validation_run_id={certificate_run_id} "
        f"startup_binding_hash={startup_binding_hash} "
        f"startup_intent_hash={startup_binding['startup_intent_hash']} "
        f"validation_process_id={validation_process_id} "
        f"requested_save_path_id={loader_path_id} "
        f"active_save_path_id={loader_path_id}\n"
    ).encode("utf-8")
    archive_prefix_payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
        for record in records[:3]
    ).encode("utf-8")
    startup_captures = {
        "runtime_config_evidence": startup_capture(runtime_payload, f"{name}-runtime"),
        "startup_binding": startup_capture(startup_binding_payload, f"{name}-binding"),
        "consumed_prefs": startup_capture(consumed_prefs_payload, f"{name}-prefs"),
        "player_log_prefix": startup_capture(player_log_payload, f"{name}-player"),
        "session_archive_prefix": startup_capture(archive_prefix_payload, f"{name}-archive"),
    }
    startup_material = startup.validate_material(
        startup_captures,
        expected_runtime_sha256=runtime_hash,
        expected_binding_sha256=startup_binding_hash,
        process_id=validation_process_id,
    )
    startup_process = startup.ProcessEvidence(
        validation_process_id,
        "OxygenNotIncluded.exe",
        canonical_hash({"process_creation": name}),
        canonical_hash({"process_image_path": name}),
        canonical_hash({"process_image": name}),
        "",
    )
    legacy_source_payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for record in records
    ).encode("utf-8")
    legacy_source_archive = {
        "session_id": session_id,
        "source_identity_id": startup_captures["session_archive_prefix"].source_identity_id,
        "file_sha256": startup.sha256_bytes(legacy_source_payload),
        "byte_length": len(legacy_source_payload),
        "record_count": len(records),
        "first_seq": 1,
        "last_seq": len(records),
        "record_chain_hash": canonical_hash([canonical_hash(record) for record in records]),
        "prefix_record_count": 3,
        "prefix_file_sha256": startup_captures["session_archive_prefix"].sha256,
        "prefix_byte_length": startup_captures["session_archive_prefix"].length,
    }
    startup_report = startup.build_report(
        startup_captures,
        startup_material,
        startup_process,
        legacy_source_archive,
        expected_runtime_sha256=runtime_hash,
        expected_binding_sha256=startup_binding_hash,
        created_at="2026-08-31T02:00:01.250000+00:00",
    )
    startup_bundle = lineage_path.parent / shutdown.STARTUP_BUNDLE_DIRECTORY
    startup.publish_bundle(startup_bundle, startup_captures, startup_report)
    if root_log:
        event_log = bridge / "state.jsonl"
        prefix = {
            "schema": "oni.event.v2",
            "event": "session_started",
            "session_id": "historical-session",
            "seq": 1,
            "ts": "2026-08-31T01:00:00+00:00",
        }
        write_jsonl(event_log, [prefix, *records])
    else:
        event_log = bridge / "sessions" / shutdown.safe_session_name(session_id) / "state.jsonl"
        write_jsonl(event_log, records)
    return {
        "case": case,
        "bridge": bridge,
        "run_id": run_id,
        "manifest": lineage_path,
        "report": report_path,
        "report_hash": report["report_hash"],
        "forward_step": forward_step,
        "inverse_step": inverse_step,
        "command_ids": command_ids,
        "bundle_hashes": [bundle[0]["bundle_hash"] for bundle in bundles],
        "event_log": event_log,
        "output": output_path,
        "records": records,
        "startup_bundle": startup_bundle,
        "startup_evidence_hash": startup_report["evidence_hash"],
        "validation_process_id": validation_process_id,
        "loader_path_text": loader_path_text,
        "startup_captures": startup_captures,
        "startup_report": startup_report,
    }


def make_fixture(
    base: pathlib.Path,
    name: str,
    *,
    root_log: bool = False,
    startup_run_id: str | None = None,
    startup_session_stage: str = "origin",
) -> dict[str, Any]:
    """Create a four-session, save-hash-rotating shutdown v3 fixture.

    The startup bundle is deliberately published after the origin session
    archive exists, but before any later session archive exists.  The origin
    archive is then completed and the activation/forward/inverse sessions are
    materialized in their real causal order.
    """

    case = base / name
    bridge = case / "ai-bridge"
    run_id = f"g0-{name}"
    certificate_run_id = startup_run_id or run_id
    lineage_path = bridge / "evidence" / "save-lineage" / run_id / "manifest.json"
    report_path = lineage_path.parent / "priority-pair-finalization.json"
    output_path = lineage_path.parent / "shutdown-evidence.json"
    session_ids = {
        "origin": f"session-{name}-origin",
        "activation": f"session-{name}-activation",
        "forward": f"session-{name}-post-forward",
        "final": f"session-{name}-post-inverse-final",
    }
    save_hashes = {
        stage: "sha256:"
        + hashlib.sha256(f"save-{name}-{stage}".encode("utf-8")).hexdigest()
        for stage in ("origin", "activation", "forward", "final")
    }
    world_id = 0
    mod_version = "0.7.0"
    game_build = "cl-fixture"
    loader_path = case / "fixture.sav"
    loader_path_text = str(loader_path)
    loader_path_id = startup.path_id_upper(loader_path_text)
    derivative_path = case / "fixture-derivative.sav"
    save_path_id = startup.path_id_upper(str(derivative_path))
    assert save_path_id != loader_path_id
    validation_process_id = 4242

    (
        startup_intent,
        runtime_evidence,
        runtime_payload,
        runtime_hash,
        startup_binding,
        startup_binding_payload,
        startup_binding_hash,
    ) = make_startup_v2_v5_material(
        fixture_name=name,
        validation_run_id=certificate_run_id,
        loader_path_id=loader_path_id,
        loader_save_sha256=save_hashes["origin"],
        loader_save_length=987654,
        game_build=game_build,
        mod_version=mod_version,
        expected_probe_dll_sha256=canonical_hash({"probe": name}),
    )

    def common(stage: str) -> dict[str, Any]:
        fields = startup_common_fields(
            startup_binding,
            startup_binding_hash,
            validation_process_id,
            session_ids[stage],
            world_id=world_id,
            save_id_hash=save_hashes[stage],
        )
        if stage != "origin":
            fields["active_save_path_id"] = save_path_id
        return fields

    def state(stage: str, seq: int, timestamp: str, snapshot_seq: int) -> dict[str, Any]:
        return {
            **common(stage),
            "schema": "oni.state.v2",
            "event": "state_snapshot",
            "seq": seq,
            "snapshot_seq": snapshot_seq,
            "snapshot_id": f"{session_ids[stage]}:{seq}",
            "ts": timestamp,
            "sample": {"interval_seconds": 5.0},
            "writer": {"dropped_snapshot_count": 0, "fault_count": 0},
        }

    def projection(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "session_id": record["session_id"],
            "snapshot_id": record["snapshot_id"],
            "seq": record["seq"],
            "save_id_hash": record["save_id_hash"],
            "world_id": record["world_id"],
            "mod_version": record["mod_version"],
            "snapshot_ts": record["ts"],
        }

    def start_records(stage: str, base_second: int) -> list[dict[str, Any]]:
        base_time = f"2026-08-31T02:00:{base_second:02d}"
        return [
            {
                **common(stage),
                "schema": "oni.event.v2",
                "event": "session_started",
                "reason": "save_loader_load_complete",
                "seq": 1,
                "ts": base_time + "+00:00",
            },
            {
                **common(stage),
                "schema": "oni.event.v2",
                "event": "save_loaded",
                "reason": "save_loader_load_complete",
                "seq": 2,
                "ts": base_time + ".250000+00:00",
            },
        ]

    def save_record(stage: str, next_stage: str, seq: int, timestamp: str) -> dict[str, Any]:
        return {
            **common(next_stage),
            "schema": "oni.event.v2",
            "event": "save_write_completed",
            "session_id": session_ids[stage],
            "seq": seq,
            "ts": timestamp,
            "success": True,
            "is_auto_save": False,
            "update_save_pointer": True,
            "file_rewrite_observed": True,
            "save_file_readable": True,
            "requested_save_path_id": save_path_id,
            "result_save_path_id": save_path_id,
            "active_save_path_id": save_path_id,
            "before_save_id_hash": save_hashes[stage],
            "written_save_id_hash": save_hashes[next_stage],
            "active_save_id_hash": save_hashes[next_stage],
        }

    def save_started_record(stage: str, seq: int, timestamp: str) -> dict[str, Any]:
        return {
            **common(stage),
            "schema": "oni.event.v2",
            "event": "save_write_started",
            "seq": seq,
            "ts": timestamp,
            "is_auto_save": False,
            "update_save_pointer": True,
            "requested_save_path_id": save_path_id,
            "before_save_id_hash": save_hashes[stage],
            "save_dirty": True,
        }

    def unload_and_end_records(
        stage: str, next_stage: str, first_seq: int, first_timestamp: str, end_timestamp: str
    ) -> list[dict[str, Any]]:
        return [
            {
                **common(next_stage),
                "schema": "oni.event.v2",
                "event": "save_unloaded",
                "reason": "save_loader_load_start",
                "session_id": session_ids[stage],
                "seq": first_seq,
                "ts": first_timestamp,
            },
            {
                **common(next_stage),
                "schema": "oni.event.v2",
                "event": "session_ended",
                "reason": "save_loader_load_start",
                "session_id": session_ids[stage],
                "seq": first_seq + 1,
                "ts": end_timestamp,
            },
        ]

    origin_snapshot_record = state("origin", 3, "2026-08-31T02:00:00.500000+00:00", 1)
    activation_snapshot_record = state(
        "activation", 3, "2026-08-31T02:00:10.500000+00:00", 1
    )
    forward_snapshot_record = state(
        "forward", 3, "2026-08-31T02:00:20.500000+00:00", 1
    )
    final_intermediate_record = state(
        "final", 3, "2026-08-31T02:00:30.500000+00:00", 1
    )
    final_snapshot_record = state("final", 4, "2026-08-31T02:00:31+00:00", 2)
    origin = projection(origin_snapshot_record)
    activation_reload = projection(activation_snapshot_record)
    forward_reload = projection(forward_snapshot_record)
    final_reload = projection(final_snapshot_record)

    origin_prefix = [*start_records("origin", 0), origin_snapshot_record]
    origin_archive = (
        bridge / "sessions" / shutdown.safe_session_name(session_ids["origin"]) / "state.jsonl"
    )
    origin_records = [
        *origin_prefix,
        save_started_record("origin", 4, "2026-08-31T02:00:01.750000+00:00"),
        save_record("origin", "activation", 5, "2026-08-31T02:00:02+00:00"),
        *unload_and_end_records(
            "origin",
            "activation",
            6,
            "2026-08-31T02:00:02.500000+00:00",
            "2026-08-31T02:00:03+00:00",
        ),
    ]
    # Real ordering: S0 is complete and archived before startup evidence is
    # frozen; no later session archive exists yet.
    write_jsonl(origin_archive, origin_records)

    consumed_prefs_payload = (
        "strings:\r\n"
        "  InstalledLanguage: en\r\n"
        f"  SaveFilenameKey/: {loader_path_text}\r\n"
        "ints:\r\n"
        "  SESSION_ID: 1\r\n"
    ).encode("utf-8")
    player_log_payload = (
        "[INFO] boot\n"
        f"[INFO] Loading save file: {loader_path_text}\n"
        f"[INFO] Loaded [{loader_path_text}]\n"
        "[ONI AI Probe] STARTUP_BINDING_VERIFIED "
        f"validation_run_id={certificate_run_id} "
        f"startup_binding_hash={startup_binding_hash} "
        f"startup_intent_hash={startup_binding['startup_intent_hash']} "
        f"validation_process_id={validation_process_id} "
        f"requested_save_path_id={loader_path_id} "
        f"active_save_path_id={loader_path_id}\n"
    ).encode("utf-8")
    startup_source_records = copy.deepcopy(origin_records)
    if startup_session_stage != "origin":
        assert startup_session_stage == "final"
        for startup_record in startup_source_records:
            startup_record["session_id"] = session_ids["final"]
            if startup_record.get("snapshot_id") is not None:
                startup_record["snapshot_id"] = (
                    f"{session_ids['final']}:{startup_record['seq']}"
                )
    startup_prefix_records = startup_source_records[:3]
    startup_source_payload = "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for record in startup_source_records
    ).encode("utf-8")
    archive_prefix_payload = b"".join(startup_source_payload.splitlines(keepends=True)[:3])
    startup_source_identity = (
        startup.identity_id(origin_archive.lstat())
        if startup_session_stage == "origin"
        else canonical_hash({"fixture_startup_source": name, "session": "final"})
    )
    startup_captures = {
        "runtime_config_evidence": startup_capture(runtime_payload, f"{name}-runtime"),
        "startup_binding": startup_capture(startup_binding_payload, f"{name}-binding"),
        "consumed_prefs": startup_capture(consumed_prefs_payload, f"{name}-prefs"),
        "player_log_prefix": startup_capture(player_log_payload, f"{name}-player"),
        "session_archive_prefix": startup.Capture(
            archive_prefix_payload,
            startup.sha256_bytes(archive_prefix_payload),
            len(archive_prefix_payload),
            startup_source_identity,
        ),
    }
    startup_material = startup.validate_material(
        startup_captures,
        expected_runtime_sha256=runtime_hash,
        expected_binding_sha256=startup_binding_hash,
        process_id=validation_process_id,
    )
    startup_process = startup.ProcessEvidence(
        validation_process_id,
        "OxygenNotIncluded.exe",
        canonical_hash({"process_creation": name}),
        canonical_hash({"process_image_path": name}),
        canonical_hash({"process_image": name}),
        "",
    )
    startup_source_archive = {
        "session_id": startup_source_records[0]["session_id"],
        "source_identity_id": startup_source_identity,
        "file_sha256": startup.sha256_bytes(startup_source_payload),
        "byte_length": len(startup_source_payload),
        "record_count": len(startup_source_records),
        "first_seq": 1,
        "last_seq": len(startup_source_records),
        "record_chain_hash": canonical_hash(
            [canonical_hash(record) for record in startup_source_records]
        ),
        "prefix_record_count": len(startup_prefix_records),
        "prefix_file_sha256": startup.sha256_bytes(archive_prefix_payload),
        "prefix_byte_length": len(archive_prefix_payload),
    }
    startup_report = startup.build_report(
        startup_captures,
        startup_material,
        startup_process,
        startup_source_archive,
        expected_runtime_sha256=runtime_hash,
        expected_binding_sha256=startup_binding_hash,
        created_at="2026-08-31T02:00:01+00:00",
    )
    startup_bundle = lineage_path.parent / shutdown.STARTUP_BUNDLE_DIRECTORY
    later_archive_paths = [
        bridge / "sessions" / shutdown.safe_session_name(session_ids[stage]) / "state.jsonl"
        for stage in ("activation", "forward", "final")
    ]
    later_archives_absent_before_bundle = (
        origin_archive.is_file()
        and read_jsonl(origin_archive) == origin_records
        and all(not path.exists() for path in later_archive_paths)
    )
    lineage_path.parent.mkdir(parents=True, exist_ok=True)
    startup.publish_bundle(startup_bundle, startup_captures, startup_report)

    activation_records = [
        *start_records("activation", 10),
        activation_snapshot_record,
        save_started_record("activation", 4, "2026-08-31T02:00:11.750000+00:00"),
        save_record("activation", "forward", 5, "2026-08-31T02:00:12+00:00"),
        *unload_and_end_records(
            "activation",
            "forward",
            6,
            "2026-08-31T02:00:12.500000+00:00",
            "2026-08-31T02:00:13+00:00",
        ),
    ]
    forward_records = [
        *start_records("forward", 20),
        forward_snapshot_record,
        save_started_record("forward", 4, "2026-08-31T02:00:21.750000+00:00"),
        save_record("forward", "final", 5, "2026-08-31T02:00:22+00:00"),
        *unload_and_end_records(
            "forward",
            "final",
            6,
            "2026-08-31T02:00:22.500000+00:00",
            "2026-08-31T02:00:23+00:00",
        ),
    ]
    records = [
        *start_records("final", 30),
        final_intermediate_record,
        final_snapshot_record,
        {
            **common("final"),
            "schema": "oni.event.v2",
            "event": "session_ended",
            "reason": "application_quit",
            "seq": 5,
            "ts": "2026-08-31T02:00:32+00:00",
        },
        {
            **common("final"),
            "schema": "oni.event.v2",
            "event": "probe_shutdown",
            "seq": 6,
            "ts": "2026-08-31T02:00:32.250000+00:00",
        },
    ]
    for path, session_records in zip(
        later_archive_paths, (activation_records, forward_records, records)
    ):
        write_jsonl(path, session_records)

    disabled_policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": False,
        "allowed_save_hash_count": 0,
        "pending_count": 0,
        "approving_count": 0,
        "approved_count": 0,
        "executing_count": 0,
        "confirming_count": 0,
        "temporary_count": 0,
    }
    activation_save = {
        key: value
        for key, value in save_record(
            "origin", "activation", 5, "2026-08-31T02:00:02+00:00"
        ).items()
        if key
        in {
            "session_id",
            "seq",
            "ts",
            "success",
            "is_auto_save",
            "update_save_pointer",
            "file_rewrite_observed",
            "save_file_readable",
            "requested_save_path_id",
            "result_save_path_id",
            "active_save_path_id",
            "before_save_id_hash",
            "written_save_id_hash",
            "active_save_id_hash",
            "save_id_hash",
            "save_dirty",
        }
    }
    initial: dict[str, Any] = {
        "kind": "activation",
        "activated_at": "2026-08-31T02:00:10.750000+00:00",
        "previous_checkpoint_hash": None,
        "source": copy.deepcopy(origin),
        "save": activation_save,
        "reload": copy.deepcopy(activation_reload),
        "policy_gate": copy.deepcopy(disabled_policy),
        "checkpoint_hash": "",
    }
    initial["checkpoint_hash"] = shutdown.lineage_contract.checkpoint_hash(initial)

    forward_step = "priority_forward"
    inverse_step = "priority_inverse"
    command_ids = [f"cmd-{name}-forward", f"cmd-{name}-inverse"]
    bundles = [_bundle(bridge, command_ids[0], 5), _bundle(bridge, command_ids[1], 3)]

    def artifact(step_index: int, step_id: str) -> dict[str, Any]:
        path = lineage_path.parent / "artifacts" / f"{step_index:02d}-{step_id}" / "fixture.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}:{step_id}\n", encoding="utf-8", newline="\n")
        return {
            "path": str(path),
            "sha256": file_sha256(path),
            "length": path.stat().st_size,
            "source_path_id": canonical_hash({"source": f"{name}:{step_id}"}),
        }

    def checkpoint(
        index: int,
        step_id: str,
        before: Mapping[str, Any],
        reload_state: Mapping[str, Any],
        previous_hash: str,
        command_id: str,
        save_timestamp: str,
    ) -> dict[str, Any]:
        next_stage = "forward" if step_id == forward_step else "final"
        before_stage = "activation" if step_id == forward_step else "forward"
        save = {
            key: value
            for key, value in save_record(before_stage, next_stage, 5, save_timestamp).items()
            if key
            in {
                "session_id",
                "seq",
                "ts",
                "success",
                "is_auto_save",
                "update_save_pointer",
                "file_rewrite_observed",
                "save_file_readable",
                "requested_save_path_id",
                "result_save_path_id",
                "active_save_path_id",
                "before_save_id_hash",
                "written_save_id_hash",
                "active_save_id_hash",
                "save_id_hash",
                "save_dirty",
            }
        }
        result: dict[str, Any] = {
            "index": index,
            "step_id": step_id,
            "scenario": f"{step_id} persists after explicit save and reload",
            "outcome": "confirmed",
            "expected_mutation": "persisted_change",
            "recorded_at": reload_state["snapshot_ts"],
            "previous_checkpoint_hash": previous_hash,
            "before": copy.deepcopy(dict(before)),
            "command": {"id": command_id, "action": shutdown.ACTION},
            "artifacts": [artifact(index, step_id)],
            "save": save,
            "reload": copy.deepcopy(dict(reload_state)),
            "policy_gate": copy.deepcopy(disabled_policy),
            "checkpoint_hash": "",
        }
        result["checkpoint_hash"] = shutdown.lineage_contract.checkpoint_hash(result)
        return result

    forward_checkpoint = checkpoint(
        1,
        forward_step,
        activation_reload,
        forward_reload,
        initial["checkpoint_hash"],
        command_ids[0],
        "2026-08-31T02:00:12+00:00",
    )
    inverse_checkpoint = checkpoint(
        2,
        inverse_step,
        forward_reload,
        final_reload,
        forward_checkpoint["checkpoint_hash"],
        command_ids[1],
        "2026-08-31T02:00:22+00:00",
    )
    latest_checkpoint_hash = inverse_checkpoint["checkpoint_hash"]
    manifest = {
        "schema": shutdown.LINEAGE_SCHEMA,
        "test_run_id": run_id,
        "status": "active",
        "source_kind": "save_as_copy",
        "expected_mod_version": mod_version,
        "reserved_at": "2026-08-31T02:00:00.750000+00:00",
        "save_path": loader_path_text,
        "save_path_id": save_path_id,
        "save_scope": "external_test",
        "save_absent_at_reservation": True,
        "reservation_source": copy.deepcopy(origin),
        "reservation_policy": copy.deepcopy(disabled_policy),
        "initial": initial,
        "checkpoints": [forward_checkpoint, inverse_checkpoint],
        "latest": copy.deepcopy(final_reload),
        "latest_checkpoint_hash": latest_checkpoint_hash,
    }
    write_json(lineage_path, manifest)

    freezes: list[tuple[dict[str, Any], pathlib.Path]] = []
    for checkpoint_record, (bundle, bundle_path) in zip(
        (forward_checkpoint, inverse_checkpoint), bundles
    ):
        step_id = str(checkpoint_record["step_id"])
        freeze: dict[str, Any] = {
            "schema": shutdown.FREEZE_SCHEMA,
            "step_id": step_id,
            "test_run_id": run_id,
            "manifest_binding": {
                "latest_checkpoint_hash": checkpoint_record["previous_checkpoint_hash"],
                "before": copy.deepcopy(checkpoint_record["before"]),
            },
            "approval_bundle": {
                "path": bundle_path.relative_to(bridge).as_posix(),
                "file_sha256": file_sha256(bundle_path),
                "bundle_hash": bundle["bundle_hash"],
                "document": bundle,
            },
            "confirmation_certificate": {"fixture": True},
            "confirmation_records": [{"event": "command_ack"}],
            "frozen_at": checkpoint_record["recorded_at"],
            "freeze_hash": "",
        }
        freeze["freeze_hash"] = shutdown._hash_without(freeze, "freeze_hash")
        freeze_path = lineage_path.parent / "priority-command-freezes" / f"{step_id}.json"
        write_json(freeze_path, freeze)
        freezes.append((freeze, freeze_path))

    report_commands: list[dict[str, Any]] = []
    for step_id, command_id, (bundle, bundle_path), (freeze, freeze_path) in zip(
        (forward_step, inverse_step), command_ids, bundles, freezes
    ):
        report_commands.append(
            {
                "step_id": step_id,
                "id": command_id,
                "action": shutdown.ACTION,
                "automatic_approval_bundle": {
                    "path": bundle_path.relative_to(bridge).as_posix(),
                    "file_sha256": file_sha256(bundle_path),
                    "bundle_hash": bundle["bundle_hash"],
                },
                "lineage_freeze": {
                    "path": str(freeze_path),
                    "file_sha256": file_sha256(freeze_path),
                    "freeze_hash": freeze["freeze_hash"],
                },
            }
        )
    report: dict[str, Any] = {
        "schema": shutdown.PAIR_REPORT_SCHEMA,
        "status": "passed",
        "classification": None,
        "verification_mode": "offline_read_only",
        "fail_closed": True,
        "manifest": {
            "path": str(lineage_path),
            "file_sha256": file_sha256(lineage_path),
            "test_run_id": run_id,
            "latest_checkpoint_hash": latest_checkpoint_hash,
        },
        "pair": {
            "forward_step": forward_step,
            "inverse_step": inverse_step,
            "commands": report_commands,
        },
        "final_tip": {
            "session_id": session_ids["final"],
            "snapshot_id": final_snapshot_record["snapshot_id"],
            "save_id_hash": save_hashes["final"],
            "state_hash": canonical_state_hash(final_snapshot_record),
        },
        "shutdown": {"actions_enabled": False, "queues": {"nonterminal": {}}},
        "report_hash": "",
    }
    report["report_hash"] = shutdown._hash_without(report, "report_hash")
    write_json(report_path, report)

    if root_log:
        event_log = bridge / "state.jsonl"
        historical = {
            "schema": "oni.event.v2",
            "event": "session_started",
            "session_id": "historical-session",
            "seq": 1,
            "ts": "2026-08-31T01:00:00+00:00",
        }
        write_jsonl(event_log, [historical, *origin_records, *activation_records, *forward_records, *records])
    else:
        event_log = later_archive_paths[-1]

    return {
        "case": case,
        "bridge": bridge,
        "run_id": run_id,
        "manifest": lineage_path,
        "report": report_path,
        "report_hash": report["report_hash"],
        "forward_step": forward_step,
        "inverse_step": inverse_step,
        "command_ids": command_ids,
        "bundle_hashes": [bundle[0]["bundle_hash"] for bundle in bundles],
        "event_log": event_log,
        "output": output_path,
        "records": records,
        "origin_archive": origin_archive,
        "origin_records": origin_records,
        "activation_archive": later_archive_paths[0],
        "session_ids": session_ids,
        "save_hashes": save_hashes,
        "save_path_id": save_path_id,
        "startup_bundle": startup_bundle,
        "startup_evidence_hash": startup_report["evidence_hash"],
        "validation_process_id": validation_process_id,
        "loader_path_text": loader_path_text,
        "startup_captures": startup_captures,
        "startup_report": startup_report,
        "startup_bundle_order_verified": later_archives_absent_before_bundle,
    }


def build(fixture: Mapping[str, Any], **test_hooks: Any) -> dict[str, Any]:
    return shutdown.build_shutdown_evidence(
        manifest_path=fixture["manifest"],
        bridge_dir=fixture["bridge"],
        pair_report_path=fixture["report"],
        expected_pair_report_hash=fixture["report_hash"],
        expected_run_id=fixture["run_id"],
        forward_step=fixture["forward_step"],
        inverse_step=fixture["inverse_step"],
        command_ids=fixture["command_ids"],
        command_bundle_hashes=fixture["bundle_hashes"],
        event_log=fixture["event_log"],
        startup_bundle_dir=fixture["startup_bundle"],
        expected_startup_evidence_hash=fixture["startup_evidence_hash"],
        expected_validation_process_id=fixture["validation_process_id"],
        **test_hooks,
    )


def publish(fixture: Mapping[str, Any]) -> dict[str, Any]:
    document = build(fixture)
    shutdown.write_evidence_create_only(fixture["output"], fixture["manifest"], document)
    return document


def verify(fixture: Mapping[str, Any], expected_hash: str, **test_hooks: Any) -> dict[str, Any]:
    return shutdown.verify_published_shutdown_evidence(
        evidence_path=fixture["output"],
        expected_evidence_hash=expected_hash,
        manifest_path=fixture["manifest"],
        bridge_dir=fixture["bridge"],
        pair_report_path=fixture["report"],
        expected_pair_report_hash=fixture["report_hash"],
        expected_run_id=fixture["run_id"],
        forward_step=fixture["forward_step"],
        inverse_step=fixture["inverse_step"],
        command_ids=fixture["command_ids"],
        command_bundle_hashes=fixture["bundle_hashes"],
        startup_bundle_dir=fixture["startup_bundle"],
        expected_startup_evidence_hash=fixture["startup_evidence_hash"],
        expected_validation_process_id=fixture["validation_process_id"],
        **test_hooks,
    )


def cli_binding_args(fixture: Mapping[str, Any]) -> list[str]:
    arguments = [
        "--manifest",
        str(fixture["manifest"]),
        "--bridge-dir",
        str(fixture["bridge"]),
        "--priority-pair-report",
        str(fixture["report"]),
        "--priority-pair-report-hash",
        str(fixture["report_hash"]),
        "--run-id",
        str(fixture["run_id"]),
        "--forward-step",
        str(fixture["forward_step"]),
        "--inverse-step",
        str(fixture["inverse_step"]),
        "--startup-consumption-bundle",
        str(fixture["startup_bundle"]),
        "--startup-consumption-evidence-hash",
        str(fixture["startup_evidence_hash"]),
        "--validation-process-id",
        str(fixture["validation_process_id"]),
    ]
    for command_id in fixture["command_ids"]:
        arguments.extend(("--command-id", str(command_id)))
    for bundle_hash in fixture["bundle_hashes"]:
        arguments.extend(("--command-bundle-hash", str(bundle_hash)))
    return arguments


def run_cli(arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-B", str(CORE), *arguments],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationFailure(f"shutdown CLI returned {completed.returncode}")
    return json.loads(completed.stdout)


def expect_block(action: Callable[[], Any], *, code: str | None = None) -> str:
    try:
        action()
    except shutdown.ShutdownEvidenceError as exc:
        if code is not None:
            assert_true(exc.code == code, f"expected blocker {code}, got {exc.code}")
        else:
            assert_true(True, "shutdown blocker observed")
        return exc.code
    raise VerificationFailure("invalid shutdown evidence was accepted")


def expect_startup_block(action: Callable[[], Any], *, code: str) -> str:
    try:
        action()
    except startup.StartupEvidenceError as exc:
        assert_true(exc.code == code, f"expected startup blocker {code}, got {exc.code}")
        return exc.code
    raise VerificationFailure("invalid startup bundle operation was accepted")


def make_file_writable(path: pathlib.Path) -> None:
    path.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_positive(base: pathlib.Path) -> dict[str, Any]:
    archive_fixture = make_fixture(base, "positive-archive")
    document = publish(archive_fixture)
    verified = verify(archive_fixture, document["evidence_hash"])
    assert_true(verified["document"] == document, "published evidence did not round-trip")
    assert_true(document["source"]["kind"] == "session_archive", "archive source kind was not frozen")
    assert_true(
        document["schema"] == "oni.normal-shutdown-evidence.v3"
        and set(document) == shutdown.EVIDENCE_KEYS
        and document["evidence_hash"] == shutdown._hash_without(document, "evidence_hash"),
        "shutdown v3 document shape or self-hash is invalid",
    )
    certificate = document["startup_consumption_certificate"]
    assert_true(
        set(certificate) == shutdown.STARTUP_CERTIFICATE_KEYS
        and set(certificate["bundle"]) == shutdown.STARTUP_CERTIFICATE_BUNDLE_KEYS
        and set(certificate["replay"]) == shutdown.STARTUP_CERTIFICATE_REPLAY_KEYS
        and set(certificate["binding"]) == shutdown.STARTUP_CERTIFICATE_BINDING_KEYS
        and set(certificate["process"]) == startup.PROCESS_EVIDENCE_KEYS
        and set(certificate["session"]) == shutdown.STARTUP_CERTIFICATE_SESSION_KEYS
        and set(certificate["source_archive"]) == startup.SOURCE_ARCHIVE_KEYS
        and set(certificate["loader"]) == shutdown.STARTUP_CERTIFICATE_LOADER_KEYS,
        "startup certificate projection is not exact-field bound",
    )
    raw_report = archive_fixture["startup_bundle"] / startup.REPORT_FILE
    assert_true(
        certificate["evidence_hash"] == archive_fixture["startup_evidence_hash"]
        and certificate["bundle"]["report_file_sha256"] == file_sha256(raw_report)
        and certificate["validation_run_id"] == archive_fixture["run_id"]
        and certificate["session"]["session_id"]
        == json.loads(archive_fixture["manifest"].read_text(encoding="utf-8"))[
            "reservation_source"
        ]["session_id"]
        and certificate["process"]["process_id"] == archive_fixture["validation_process_id"],
        "startup certificate hash/report/lineage/process anchors are incomplete",
    )
    manifest = json.loads(archive_fixture["manifest"].read_text(encoding="utf-8"))
    origin_archive_projection = document["lineage"]["origin_archive"]
    activation_reload_projection = document["lineage"]["activation_reload_archive"]
    assert_true(
        manifest["reservation_source"] == manifest["initial"]["source"]
        and certificate["session"]["session_id"]
        == archive_fixture["session_ids"]["origin"]
        and document["final_tip"]["session_id"] == archive_fixture["session_ids"]["final"]
        and len(set(archive_fixture["session_ids"].values())) == 4
        and len(set(archive_fixture["save_hashes"].values())) == 4
        and manifest["save_path_id"] != certificate["binding"]["requested_save_path_id"]
        and origin_archive_projection["activation_save_event"]
        == manifest["initial"]["save"]
        and origin_archive_projection["file_sha256"]
        == certificate["source_archive"]["file_sha256"]
        and origin_archive_projection["record_chain_hash"]
        == certificate["source_archive"]["record_chain_hash"]
        and activation_reload_projection["origin_end_before_start"] is True
        and archive_fixture["startup_bundle_order_verified"] is True,
        "four-session origin-to-final lineage or startup publication order is invalid",
    )
    replay = startup.verify_command(
        type("ReplayArgs", (), {"bundle_dir": str(archive_fixture["startup_bundle"])})()
    )
    assert_true(
        replay["status"] == "passed"
        and replay["verified_evidence_hash"] == archive_fixture["startup_evidence_hash"],
        "create-only startup bundle replay did not pass",
    )
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True)
    assert_true(
        archive_fixture["loader_path_text"] not in serialized
        and "SaveFilenameKey/:" not in serialized
        and "[INFO] Loading save file:" not in serialized,
        "raw startup prefs/log/loader path leaked into shutdown evidence",
    )
    root_fixture = make_fixture(base, "positive-root", root_log=True)
    root_document = publish(root_fixture)
    verify(root_fixture, root_document["evidence_hash"])
    assert_true(root_document["source"]["kind"] == "root_log", "root source kind was not frozen")
    cli_fixture = make_fixture(base, "positive-cli")
    created = run_cli(
        [
            "create",
            *cli_binding_args(cli_fixture),
            "--event-log",
            str(cli_fixture["event_log"]),
            "--output",
            str(cli_fixture["output"]),
        ]
    )
    assert_true(created.get("status") == "published", "create CLI did not publish evidence")
    checked = run_cli(
        [
            "verify",
            *cli_binding_args(cli_fixture),
            "--shutdown-evidence",
            str(cli_fixture["output"]),
            "--shutdown-evidence-hash",
            str(created["evidence_hash"]),
        ]
    )
    assert_true(checked.get("status") == "passed", "verify CLI did not accept its publication")
    cli_serialized = json.dumps({"create": created, "verify": checked}, ensure_ascii=False)
    assert_true(
        cli_fixture["loader_path_text"] not in cli_serialized
        and str(cli_fixture["output"]) not in cli_serialized
        and "SaveFilenameKey/:" not in cli_serialized
        and "[INFO] Loading save file:" not in cli_serialized,
        "raw startup prefs/log/loader path leaked to shutdown CLI stdout",
    )
    pair_report = json.loads(cli_fixture["report"].read_text(encoding="utf-8"))
    relative_bundle_paths = [
        item["automatic_approval_bundle"]["path"] for item in pair_report["pair"]["commands"]
    ]
    assert_true(
        all(not pathlib.PureWindowsPath(value).is_absolute() and "\\" not in value for value in relative_bundle_paths),
        "positive fixture did not use the production bridge-relative bundle path contract",
    )
    return {
        "session_archive": "passed",
        "root_log": "passed",
        "create_and_verify_cli": "passed",
        "required_order": ["session_ended", "probe_shutdown"],
        "typed_canonical_document_hash": "present_and_verified",
        "bundle_paths": "bridge_relative_production_shape",
        "startup_bundle_replay": "passed",
        "startup_bundle_publication_order": "origin_archive_before_bundle_before_later_archives",
        "lineage_session_count": 4,
        "rotated_save_hash_count": 4,
        "startup_certificate_projection": "exact_fields_and_self_hashed",
        "startup_certificate_evidence_hash": certificate["evidence_hash"],
        "startup_raw_report_file_sha256": certificate["bundle"]["report_file_sha256"],
        "shutdown_evidence_hash": document["evidence_hash"],
        "shutdown_evidence_file_sha256": verified["file_sha256"],
        "shutdown_source_file_sha256": document["source"]["file_sha256"],
        "raw_startup_material_in_json_or_stdout": False,
    }


def _mutated_log_case(
    base: pathlib.Path,
    name: str,
    mutate: Callable[[list[dict[str, Any]]], None],
    *,
    code: str | None = None,
) -> str:
    fixture = make_fixture(base, name)
    records = read_jsonl(fixture["event_log"])
    mutate(records)
    write_jsonl(fixture["event_log"], records)
    blocker = expect_block(lambda: build(fixture), code=code)
    assert_true(not fixture["output"].exists(), "failed creation published an output")
    return blocker


def test_runtime_identity_and_start_envelope(base: pathlib.Path) -> dict[str, Any]:
    def wrong_build(records: list[dict[str, Any]]) -> None:
        records[-2]["game_build"] = "cl-attacker"
        records[-1]["game_build"] = "cl-attacker"

    build_blocker = _mutated_log_case(
        base,
        "wrong-build-tail-pair",
        wrong_build,
        code="shutdown_event_build_mismatch",
    )

    def wrong_mod(records: list[dict[str, Any]]) -> None:
        for record in records:
            record["mod_version"] = "9.9.9-attacker"

    mod_blocker = _mutated_log_case(
        base,
        "wrong-mod-entire-session",
        wrong_mod,
        code="final_tip_mod_version_mismatch",
    )

    start_mutations: dict[str, Callable[[dict[str, Any]], None]] = {
        "schema": lambda record: record.__setitem__("schema", "oni.event.attacker"),
        "reason": lambda record: record.__setitem__("reason", "game_lifecycle_patch"),
        "save": lambda record: record.__setitem__("save_id_hash", "sha256:" + "0" * 64),
        "world": lambda record: record.__setitem__("world_id", 99),
        "dirty": lambda record: record.__setitem__("save_dirty", True),
        "build": lambda record: record.__setitem__("game_build", "cl-attacker"),
        "mod": lambda record: record.__setitem__("mod_version", "9.9.9-attacker"),
    }
    start_blockers: dict[str, str] = {}
    for field, mutate_start in start_mutations.items():
        def mutate(records: list[dict[str, Any]], action: Callable[[dict[str, Any]], None] = mutate_start) -> None:
            action(records[0])

        start_blockers[field] = _mutated_log_case(
            base,
            f"bad-start-{field}",
            mutate,
            code="trusted_session_start_envelope_invalid",
        )

    tip_schema = _mutated_log_case(
        base,
        "bad-final-tip-schema",
        lambda records: records[3].__setitem__("schema", "oni.event.v2"),
        code="final_tip_snapshot_identity_mismatch",
    )
    tail_schema = _mutated_log_case(
        base,
        "bad-shutdown-tail-schema",
        lambda records: records[-1].__setitem__("schema", "oni.state.v2"),
        code="shutdown_event_schema_invalid",
    )

    manifest_fixture = make_fixture(base, "manifest-latest-mod-mismatch")
    manifest = json.loads(manifest_fixture["manifest"].read_text(encoding="utf-8"))
    manifest["latest"]["mod_version"] = "9.9.9-attacker"
    write_json(manifest_fixture["manifest"], manifest)
    manifest_mod = expect_block(
        lambda: build(manifest_fixture),
        code="lineage_latest_pointer_invalid",
    )
    intermediate_blockers: dict[str, dict[str, str]] = {}
    runtime_attacks: dict[str, Any] = {
        "save": "sha256:" + "0" * 64,
        "world": 99,
        "build": "cl-attacker",
        "mod": "9.9.9-attacker",
        "dirty": True,
    }
    for record_kind, record_index in {"state_snapshot": 2, "ordinary_event": 1}.items():
        intermediate_blockers[record_kind] = {}
        for field, attack_value in runtime_attacks.items():
            key = {
                "save": "save_id_hash",
                "world": "world_id",
                "build": "game_build",
                "mod": "mod_version",
                "dirty": "save_dirty",
            }[field]

            def mutate_intermediate(
                records: list[dict[str, Any]],
                index: int = record_index,
                field_name: str = key,
                value: Any = attack_value,
            ) -> None:
                records[index][field_name] = value

            intermediate_blockers[record_kind][field] = _mutated_log_case(
                base,
                f"bad-{record_kind}-{field}",
                mutate_intermediate,
                code="shutdown_record_runtime_identity_mismatch",
            )
    return {
        "wrong_tail_build_pair": build_blocker,
        "wrong_all_record_mod": mod_blocker,
        "bad_start_envelope_fields": start_blockers,
        "bad_final_tip_schema": tip_schema,
        "bad_shutdown_tail_schema": tail_schema,
        "manifest_expected_latest_mod_mismatch": manifest_mod,
        "intermediate_record_identity": intermediate_blockers,
    }


def test_startup_record_binding_refusals(base: pathlib.Path) -> dict[str, Any]:
    attacks: dict[str, Any] = {
        "validation_run_id": "attacker-run",
        "startup_binding_hash": "sha256:" + "01" * 32,
        "startup_intent_hash": "sha256:" + "02" * 32,
        "runtime_config_evidence_sha256": "sha256:" + "03" * 32,
        "validation_process_id": 999999,
        "startup_binding_status": "pending_request",
        "requested_save_path_id": "sha256:" + "04" * 32,
        "active_save_path_id": "sha256:" + "05" * 32,
        "game_build": "cl-attacker",
        "mod_version": "9.9.9-attacker",
    }
    blockers: dict[str, str] = {}
    for index, (field, value) in enumerate(attacks.items(), start=1):
        expected = (
            "shutdown_record_runtime_identity_mismatch"
            if field in {"game_build", "mod_version"}
            else (
                "shutdown_record_save_path_binding_mismatch"
                if field in {"requested_save_path_id", "active_save_path_id"}
                else "shutdown_record_startup_binding_mismatch"
            )
        )
        blockers[field] = _mutated_log_case(
            base,
            f"startup-rec-{index:02d}",
            lambda records, key=field, attack=value: records[2].__setitem__(key, attack),
            code=expected,
        )

    first_sequence = _mutated_log_case(
        base,
        "startup-first-sequence",
        lambda records: records[0].__setitem__("seq", 2),
        code="shutdown_sequence_not_gap_free",
    )
    first_reason = _mutated_log_case(
        base,
        "startup-first-reason",
        lambda records: records[0].__setitem__("reason", "game_lifecycle_patch"),
        code="trusted_session_start_envelope_invalid",
    )
    first_save = _mutated_log_case(
        base,
        "startup-first-save",
        lambda records: records[0].__setitem__("save_id_hash", "sha256:" + "06" * 32),
        code="trusted_session_start_envelope_invalid",
    )
    prefix_fixture = make_fixture(base, "startup-prefix-continuity")
    prefix_records = read_jsonl(prefix_fixture["origin_archive"])
    prefix_records[1]["reason"] = "tampered_after_certificate"
    write_jsonl(prefix_fixture["origin_archive"], prefix_records)
    prefix = expect_block(
        lambda: build(prefix_fixture), code="startup_origin_archive_prefix_mismatch"
    )
    prefix_bytes_fixture = make_fixture(base, "startup-prefix-bytes")
    prefix_records = read_jsonl(prefix_bytes_fixture["origin_archive"])
    prefix_lines = prefix_bytes_fixture["origin_archive"].read_bytes().splitlines(keepends=True)
    prefix_lines[0] = (
        json.dumps(prefix_records[0], ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    prefix_bytes_fixture["origin_archive"].write_bytes(b"".join(prefix_lines))
    prefix_bytes = expect_block(
        lambda: build(prefix_bytes_fixture),
        code="startup_origin_archive_prefix_bytes_mismatch",
    )
    incomplete_fixture = make_fixture(base, "shutdown-log-truncated")
    incomplete_payload = incomplete_fixture["event_log"].read_bytes()
    incomplete_fixture["event_log"].write_bytes(incomplete_payload[:-1])
    incomplete = expect_block(
        lambda: build(incomplete_fixture), code="shutdown_log_incomplete_line"
    )
    return {
        "every_target_record_anchor": blockers,
        "first_session_started_seq": first_sequence,
        "first_session_started_reason": first_reason,
        "first_session_started_initial_save": first_save,
        "certificate_archive_prefix_continuity": prefix,
        "certificate_archive_prefix_raw_bytes": prefix_bytes,
        "shutdown_log_truncated_line": incomplete,
    }


def test_origin_suffix_and_activation_reload_refusals(base: pathlib.Path) -> dict[str, Any]:
    def origin_case(
        name: str,
        mutate: Callable[[list[dict[str, Any]]], None],
        code: str,
    ) -> str:
        fixture = make_fixture(base, name)
        rows = read_jsonl(fixture["origin_archive"])
        mutate(rows)
        write_jsonl(fixture["origin_archive"], rows)
        return expect_block(lambda: build(fixture), code=code)

    completion_path = origin_case(
        "sx-path",
        lambda rows: rows[4].__setitem__(
            "requested_save_path_id", "sha256:" + "11" * 32
        ),
        "startup_origin_archive_activation_save_mismatch",
    )
    completion_hash = origin_case(
        "sx-hash",
        lambda rows: rows[4].__setitem__(
            "written_save_id_hash", "sha256:" + "12" * 32
        ),
        "startup_origin_archive_activation_save_mismatch",
    )
    completion_flag = origin_case(
        "sx-flag",
        lambda rows: rows[4].__setitem__("success", False),
        "startup_origin_archive_activation_save_mismatch",
    )
    start_target = origin_case(
        "sx-start",
        lambda rows: rows[3].__setitem__(
            "requested_save_path_id", "sha256:" + "13" * 32
        ),
        "startup_origin_archive_save_start_invalid",
    )
    start_order = origin_case(
        "sx-order",
        lambda rows: rows[3].__setitem__(
            "ts", "2026-08-31T02:00:00.500000+00:00"
        ),
        "startup_origin_archive_save_start_invalid",
    )
    suffix_active = origin_case(
        "sx-active",
        lambda rows: rows[5].__setitem__(
            "active_save_path_id", "sha256:" + "14" * 32
        ),
        "startup_origin_archive_suffix_identity_mismatch",
    )
    suffix_hash = origin_case(
        "sx-save",
        lambda rows: rows[5].__setitem__("save_id_hash", "sha256:" + "15" * 32),
        "startup_origin_archive_suffix_identity_mismatch",
    )

    def duplicate_completion(rows: list[dict[str, Any]]) -> None:
        duplicate = copy.deepcopy(rows[4])
        duplicate["seq"] = 6
        duplicate["ts"] = "2026-08-31T02:00:02.250000+00:00"
        rows.insert(5, duplicate)
        rows[6]["seq"] = 7
        rows[7]["seq"] = 8

    duplicate = origin_case(
        "sx-dup",
        duplicate_completion,
        "startup_origin_archive_activation_save_mismatch",
    )

    def remove_completion(rows: list[dict[str, Any]]) -> None:
        del rows[4]
        for seq, row in enumerate(rows, start=1):
            row["seq"] = seq

    missing = origin_case(
        "sx-miss",
        remove_completion,
        "startup_origin_archive_suffix_identity_mismatch",
    )
    bad_end = origin_case(
        "sx-end",
        lambda rows: rows[-1].__setitem__("reason", "application_quit"),
        "startup_origin_archive_session_end_invalid",
    )

    source_projection_blockers: dict[str, str] = {}
    for index, field in enumerate(
        ("file_sha256", "record_chain_hash", "source_identity_id"), start=1
    ):
        fixture = make_fixture(base, f"src-{index}")
        report_path = fixture["startup_bundle"] / startup.REPORT_FILE
        make_file_writable(report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["source_archive"][field] = "sha256:" + f"{20 + index:02x}" * 32
        report["evidence_hash"] = canonical_hash(
            {key: value for key, value in report.items() if key != "evidence_hash"}
        )
        write_json(report_path, report)
        fixture["startup_evidence_hash"] = report["evidence_hash"]
        source_projection_blockers[field] = expect_block(
            lambda fixture=fixture: build(fixture),
            code="startup_origin_archive_custody_mismatch",
        )

    def reload_case(
        name: str,
        mutate: Callable[[list[dict[str, Any]]], None],
        code: str,
    ) -> str:
        fixture = make_fixture(base, name)
        rows = read_jsonl(fixture["activation_archive"])
        mutate(rows)
        write_jsonl(fixture["activation_archive"], rows)
        return expect_block(lambda: build(fixture), code=code)

    reload_requested = reload_case(
        "s1-req",
        lambda rows: rows[0].__setitem__(
            "requested_save_path_id", rows[0]["active_save_path_id"]
        ),
        "activation_reload_prefix_identity_mismatch",
    )
    reload_active = reload_case(
        "s1-active",
        lambda rows: rows[0].__setitem__(
            "active_save_path_id", "sha256:" + "31" * 32
        ),
        "activation_reload_prefix_identity_mismatch",
    )
    reload_hash = reload_case(
        "s1-hash",
        lambda rows: rows[0].__setitem__("save_id_hash", "sha256:" + "32" * 32),
        "activation_reload_prefix_identity_mismatch",
    )
    reload_time = reload_case(
        "s1-time",
        lambda rows: rows[0].__setitem__("ts", "2026-08-31T02:00:03+00:00"),
        "activation_reload_did_not_follow_origin_end",
    )
    reload_end = reload_case(
        "s1-end",
        lambda rows: rows[-1].__setitem__("reason", "application_quit"),
        "activation_reload_archive_session_end_invalid",
    )
    return {
        "completion_path": completion_path,
        "completion_hash": completion_hash,
        "completion_flag": completion_flag,
        "save_start_target": start_target,
        "save_start_after_snapshot": start_order,
        "suffix_active_path": suffix_active,
        "suffix_save_hash": suffix_hash,
        "duplicate_completion": duplicate,
        "missing_completion": missing,
        "origin_end_reason": bad_end,
        "source_archive_projection": source_projection_blockers,
        "activation_reload_requested_loader": reload_requested,
        "activation_reload_derivative_active": reload_active,
        "activation_reload_save_hash": reload_hash,
        "activation_reload_after_origin_end": reload_time,
        "activation_reload_session_end": reload_end,
    }


def test_cross_session_lineage_refusals(base: pathlib.Path) -> dict[str, Any]:
    origin_fixture = make_fixture(base, "reservation-source-mismatch")
    origin_manifest = json.loads(origin_fixture["manifest"].read_text(encoding="utf-8"))
    origin_manifest["reservation_source"]["world_id"] = 1
    write_json(origin_fixture["manifest"], origin_manifest)
    origin_mismatch = expect_block(
        lambda: build(origin_fixture), code="lineage_origin_sources_mismatch"
    )

    activation_fixture = make_fixture(base, "activation-before-hash-mismatch")
    activation_manifest = json.loads(
        activation_fixture["manifest"].read_text(encoding="utf-8")
    )
    activation_manifest["initial"]["save"]["before_save_id_hash"] = "sha256:" + "a" * 64
    rehash_lineage_chain(activation_manifest)
    write_json(activation_fixture["manifest"], activation_manifest)
    activation_before = expect_block(
        lambda: build(activation_fixture), code="lineage_activation_transition_invalid"
    )

    broken_fixture = make_fixture(base, "cross-session-chain-broken")
    broken_manifest = json.loads(broken_fixture["manifest"].read_text(encoding="utf-8"))
    broken_manifest["checkpoints"][1]["before"] = copy.deepcopy(
        broken_manifest["initial"]["reload"]
    )
    rehash_lineage_chain(broken_manifest)
    write_json(broken_fixture["manifest"], broken_manifest)
    broken_chain = expect_block(
        lambda: build(broken_fixture), code="lineage_checkpoint_hash_chain_invalid"
    )

    final_fixture = make_fixture(base, "final-session-completely-wrong")
    final_records = read_jsonl(final_fixture["event_log"])
    for record in final_records:
        record["session_id"] = "session-attacker-final"
    write_jsonl(final_fixture["event_log"], final_records)
    final_session = expect_block(lambda: build(final_fixture))

    checkpoint_command_fixture = make_fixture(base, "checkpoint-command-cross-binding")
    checkpoint_manifest = json.loads(
        checkpoint_command_fixture["manifest"].read_text(encoding="utf-8")
    )
    checkpoint_manifest["checkpoints"][0]["command"]["id"] = "cmd-attacker"
    rehash_lineage_chain(checkpoint_manifest)
    write_json(checkpoint_command_fixture["manifest"], checkpoint_manifest)
    checkpoint_report = json.loads(
        checkpoint_command_fixture["report"].read_text(encoding="utf-8")
    )
    checkpoint_report["manifest"]["file_sha256"] = file_sha256(
        checkpoint_command_fixture["manifest"]
    )
    checkpoint_report["manifest"]["latest_checkpoint_hash"] = checkpoint_manifest[
        "latest_checkpoint_hash"
    ]
    checkpoint_report["report_hash"] = shutdown._hash_without(
        checkpoint_report, "report_hash"
    )
    write_json(checkpoint_command_fixture["report"], checkpoint_report)
    checkpoint_command_fixture["report_hash"] = checkpoint_report["report_hash"]
    checkpoint_command = expect_block(
        lambda: build(checkpoint_command_fixture),
        code="priority_checkpoint_command_cross_binding_invalid",
    )

    freeze_fixture = make_fixture(base, "freeze-manifest-cross-binding")
    freeze_report = json.loads(freeze_fixture["report"].read_text(encoding="utf-8"))
    freeze_path = pathlib.Path(freeze_report["pair"]["commands"][0]["lineage_freeze"]["path"])
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze["manifest_binding"]["latest_checkpoint_hash"] = "sha256:" + "b" * 64
    freeze["freeze_hash"] = shutdown._hash_without(freeze, "freeze_hash")
    write_json(freeze_path, freeze)
    freeze_report["pair"]["commands"][0]["lineage_freeze"]["file_sha256"] = file_sha256(
        freeze_path
    )
    freeze_report["pair"]["commands"][0]["lineage_freeze"]["freeze_hash"] = freeze[
        "freeze_hash"
    ]
    freeze_report["report_hash"] = shutdown._hash_without(freeze_report, "report_hash")
    write_json(freeze_fixture["report"], freeze_report)
    freeze_fixture["report_hash"] = freeze_report["report_hash"]
    freeze_binding = expect_block(
        lambda: build(freeze_fixture), code="command_freeze_manifest_binding_invalid"
    )

    same_session_fixture = _make_same_session_legacy_fixture(
        base, "old-same"
    )
    same_session = expect_block(
        lambda: build(same_session_fixture), code="lineage_reservation_contract_invalid"
    )

    v2_fixture = make_fixture(base, "old-v2")
    v3_document = publish(v2_fixture)
    v2_document = copy.deepcopy(v3_document)
    v2_document["schema"] = "oni.normal-shutdown-evidence.v2"
    v2_document["evidence_hash"] = shutdown._hash_without(v2_document, "evidence_hash")
    make_file_writable(v2_fixture["output"])
    write_json(v2_fixture["output"], v2_document)
    legacy_v2 = expect_block(
        lambda: verify(v2_fixture, v2_document["evidence_hash"]),
        code="shutdown_evidence_shape_invalid",
    )
    return {
        "reservation_source_vs_initial_source": origin_mismatch,
        "activation_before_save_hash": activation_before,
        "cross_session_checkpoint_chain": broken_chain,
        "final_event_log_session": final_session,
        "checkpoint_command_binding": checkpoint_command,
        "freeze_manifest_binding": freeze_binding,
        "legacy_same_session_pseudo_closure": same_session,
        "legacy_v2_document": legacy_v2,
    }


def test_origin_archive_custody_refusals(base: pathlib.Path) -> dict[str, Any]:
    missing_fixture = make_fixture(base, "origin-archive-missing")
    missing_fixture["origin_archive"].unlink()
    missing = expect_block(lambda: build(missing_fixture))

    hardlink_fixture = make_fixture(base, "origin-archive-hardlink")
    hardlink_path = hardlink_fixture["origin_archive"]
    hardlink_source = hardlink_fixture["case"] / "origin-archive-source.jsonl"
    hardlink_source.write_bytes(hardlink_path.read_bytes())
    hardlink_path.unlink()
    os.link(hardlink_source, hardlink_path)
    hardlink = expect_block(lambda: build(hardlink_fixture), code="evidence_file_hardlinked")

    reparse_fixture = make_fixture(base, "origin-archive-reparse")
    session_dir = reparse_fixture["origin_archive"].parent
    backing_dir = reparse_fixture["case"] / "origin-archive-backing"
    os.rename(session_dir, backing_dir)
    junction = subprocess.run(
        [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(session_dir),
            str(backing_dir),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert_true(junction.returncode == 0, "origin archive junction fixture could not be created")
    reparse = expect_block(
        lambda: build(reparse_fixture), code="evidence_path_contains_reparse"
    )

    replacement_fixture = make_fixture(base, "origin-archive-name-swap")
    replacement_path = replacement_fixture["case"] / "same-content-origin.jsonl"
    replacement_path.write_bytes(replacement_fixture["origin_archive"].read_bytes())

    def swap_origin_after_read(_projection: Mapping[str, Any]) -> None:
        os.replace(replacement_path, replacement_fixture["origin_archive"])

    name_swap = expect_block(
        lambda: build(
            replacement_fixture,
            after_startup_session_log_read_hook=swap_origin_after_read,
        ),
        code="startup_origin_archive_changed_across_validation",
    )

    mutation_fixture = make_fixture(base, "origin-archive-window-mutation")

    def mutate_origin_after_read(_projection: Mapping[str, Any]) -> None:
        with mutation_fixture["origin_archive"].open("ab") as handle:
            handle.write(b"\n")

    window_mutation = expect_block(
        lambda: build(
            mutation_fixture,
            after_startup_session_log_read_hook=mutate_origin_after_read,
        ),
        code="startup_origin_archive_changed_across_validation",
    )
    return {
        "missing": missing,
        "hardlink": hardlink,
        "reparse": reparse,
        "same_content_path_replacement": name_swap,
        "cross_window_byte_mutation": window_mutation,
    }


def _mutated_report_bundle_path_case(
    base: pathlib.Path,
    name: str,
    value: Callable[[dict[str, Any], Mapping[str, Any]], str],
    *,
    frozen: bool,
    code: str,
) -> str:
    fixture = make_fixture(base, name)
    report = json.loads(fixture["report"].read_text(encoding="utf-8"))
    commands = report["pair"]["commands"]
    selected = commands[0]
    replacement = value(selected, commands[1])
    if frozen:
        freeze_path = pathlib.Path(selected["lineage_freeze"]["path"])
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        freeze["approval_bundle"]["path"] = replacement
        freeze["freeze_hash"] = shutdown._hash_without(freeze, "freeze_hash")
        write_json(freeze_path, freeze)
        selected["lineage_freeze"]["file_sha256"] = file_sha256(freeze_path)
        selected["lineage_freeze"]["freeze_hash"] = freeze["freeze_hash"]
    else:
        selected["automatic_approval_bundle"]["path"] = replacement
    report["report_hash"] = shutdown._hash_without(report, "report_hash")
    write_json(fixture["report"], report)
    fixture["report_hash"] = report["report_hash"]
    blocker = expect_block(lambda: build(fixture), code=code)
    assert_true(not fixture["output"].exists(), "unsafe bundle path published shutdown evidence")
    return blocker


def test_bridge_relative_path_contract(base: pathlib.Path) -> dict[str, Any]:
    report_escape = _mutated_report_bundle_path_case(
        base,
        "report-relative-escape",
        lambda _selected, _other: "../outside/approval-bundle.json",
        frozen=False,
        code="bridge_relative_path_invalid",
    )
    report_absolute = _mutated_report_bundle_path_case(
        base,
        "report-absolute-wrong-root",
        lambda _selected, _other: str(base / "wrong-root" / "approval-bundle.json"),
        frozen=False,
        code="bridge_relative_path_invalid",
    )
    report_swap = _mutated_report_bundle_path_case(
        base,
        "report-bundle-path-swap",
        lambda _selected, other: str(other["automatic_approval_bundle"]["path"]),
        frozen=False,
        code="command_bundle_report_binding_invalid",
    )
    freeze_escape = _mutated_report_bundle_path_case(
        base,
        "freeze-relative-escape",
        lambda _selected, _other: "../outside/approval-bundle.json",
        frozen=True,
        code="bridge_relative_path_invalid",
    )
    freeze_absolute = _mutated_report_bundle_path_case(
        base,
        "freeze-absolute-wrong-root",
        lambda _selected, _other: str(base / "wrong-root" / "approval-bundle.json"),
        frozen=True,
        code="bridge_relative_path_invalid",
    )
    freeze_swap = _mutated_report_bundle_path_case(
        base,
        "freeze-bundle-path-swap",
        lambda _selected, other: str(other["automatic_approval_bundle"]["path"]),
        frozen=True,
        code="command_freeze_bundle_binding_invalid",
    )
    return {
        "reported_escape": report_escape,
        "reported_absolute_wrong_root": report_absolute,
        "reported_forward_inverse_swap": report_swap,
        "frozen_escape": freeze_escape,
        "frozen_absolute_wrong_root": freeze_absolute,
        "frozen_forward_inverse_swap": freeze_swap,
    }


def test_descriptor_identity_and_link_refusals(base: pathlib.Path) -> dict[str, Any]:
    hardlink_root = base / "hardlink"
    hardlink_root.mkdir(parents=True)
    hardlink_source = hardlink_root / "source.json"
    hardlink_alias = hardlink_root / "alias.json"
    write_json(hardlink_source, {"value": "trusted"})
    os.link(hardlink_source, hardlink_alias)
    hardlink = expect_block(
        lambda: shutdown._strict_object_snapshot(hardlink_source, "hardlink fixture"),
        code="evidence_file_hardlinked",
    )

    swap_root = base / "name-swap"
    swap_root.mkdir(parents=True)
    swap_source = swap_root / "source.json"
    swap_replacement = swap_root / "replacement.json"
    write_json(swap_source, {"value": "trusted-a"})
    write_json(swap_replacement, {"value": "attack--b"})
    source_stamp = swap_source.stat().st_mtime_ns
    os.utime(swap_replacement, ns=(source_stamp, source_stamp))

    def swap_before_open() -> None:
        os.replace(swap_replacement, swap_source)

    name_swap = expect_block(
        lambda: shutdown._strict_object_snapshot(
            swap_source,
            "name-swap fixture",
            before_open_hook=swap_before_open,
        ),
        code="evidence_file_name_swapped",
    )

    change_root = base / "post-open-change"
    change_root.mkdir(parents=True)
    change_source = change_root / "source.json"
    write_json(change_source, {"value": "trusted-a"})

    def mutate_after_open() -> None:
        write_json(change_source, {"value": "attack--b"})
        changed_stamp = change_source.stat().st_mtime_ns + 10_000_000_000
        os.utime(change_source, ns=(changed_stamp, changed_stamp))

    post_open = expect_block(
        lambda: shutdown._strict_object_snapshot(
            change_source,
            "post-open-change fixture",
            after_open_hook=mutate_after_open,
        ),
        code="evidence_file_changed_or_name_swapped",
    )
    return {
        "hardlink": hardlink,
        "same_size_mtime_name_swap": name_swap,
        "post_open_content_change": post_open,
        "identity_fields": ["st_dev", "st_ino", "file_type", "st_nlink", "st_size", "st_mtime_ns"],
    }


def test_create_only_publication_ownership(base: pathlib.Path) -> dict[str, Any]:
    hardlink_fixture = make_fixture(base, "publication-hardlink")
    hardlink_document = build(hardlink_fixture)
    hardlink_alias = hardlink_fixture["case"] / "publication-hardlink-alias.json"

    def link_after_open(path: pathlib.Path) -> None:
        os.link(path, hardlink_alias)

    hardlink = expect_block(
        lambda: shutdown.write_evidence_create_only(
            hardlink_fixture["output"],
            hardlink_fixture["manifest"],
            hardlink_document,
            after_open_hook=link_after_open,
        ),
        code="shutdown_evidence_publication_outcome_unknown",
    )
    assert_true(
        hardlink_fixture["output"].exists()
        and hardlink_alias.exists()
        and os.path.samefile(hardlink_fixture["output"], hardlink_alias),
        "hardlinked publication was deleted despite ambiguous ownership",
    )

    swap_fixture = make_fixture(base, "publication-name-swap")
    swap_document = build(swap_fixture)
    replacement = swap_fixture["case"] / "external-replacement.json"
    owned_original = swap_fixture["case"] / "owned-publication-moved.json"
    write_json(replacement, {"external_replacement": True})
    replacement_bytes = replacement.read_bytes()

    def swap_after_close(path: pathlib.Path) -> None:
        os.replace(path, owned_original)
        os.replace(replacement, path)

    name_swap = expect_block(
        lambda: shutdown.write_evidence_create_only(
            swap_fixture["output"],
            swap_fixture["manifest"],
            swap_document,
            after_close_hook=swap_after_close,
        ),
        code="shutdown_evidence_publication_outcome_unknown",
    )
    assert_true(
        swap_fixture["output"].read_bytes() == replacement_bytes and owned_original.exists(),
        "after-close replacement was deleted or overwritten",
    )

    post_hash_fixture = make_fixture(base, "publication-post-hash-failure")
    post_hash_document = build(post_hash_fixture)

    def fail_after_hash(_path: pathlib.Path) -> None:
        raise RuntimeError("deterministic post-hash failure")

    try:
        shutdown.write_evidence_create_only(
            post_hash_fixture["output"],
            post_hash_fixture["manifest"],
            post_hash_document,
            after_hash_hook=fail_after_hash,
        )
    except RuntimeError:
        post_hash = "owned_output_removed"
    else:
        raise VerificationFailure("post-hash publication failure was accepted")
    assert_true(
        not post_hash_fixture["output"].exists(),
        "owned output survived a deterministic post-hash failure",
    )

    late_fixture = make_fixture(base, "publication-late-revoke-swap")
    late_document = build(late_fixture)
    late_replacement = late_fixture["case"] / "late-external-replacement.json"
    late_owned = late_fixture["case"] / "late-owned-publication.json"
    write_json(late_replacement, {"late_external_replacement": True})
    late_bytes = late_replacement.read_bytes()

    def fail_for_late_revoke(_path: pathlib.Path) -> None:
        raise RuntimeError("enter deterministic revoke path")

    def swap_before_revoke(path: pathlib.Path) -> None:
        os.replace(path, late_owned)
        os.replace(late_replacement, path)

    late_swap = expect_block(
        lambda: shutdown.write_evidence_create_only(
            late_fixture["output"],
            late_fixture["manifest"],
            late_document,
            after_hash_hook=fail_for_late_revoke,
            before_revoke_hook=swap_before_revoke,
        ),
        code="shutdown_evidence_publication_outcome_unknown",
    )
    assert_true(
        late_fixture["output"].read_bytes() == late_bytes and late_owned.exists(),
        "late revoke replacement was deleted or overwritten",
    )

    parent_fixture = make_fixture(base, "publication-parent-reparse")
    parent_document = build(parent_fixture)
    publication_parent = parent_fixture["output"].parent
    publication_backing = parent_fixture["case"] / "publication-parent-backing"

    def replace_parent_with_junction(_path: pathlib.Path) -> None:
        os.rename(publication_parent, publication_backing)
        junction = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(publication_parent),
                str(publication_backing),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert_true(junction.returncode == 0, "publication parent junction fixture failed")

    parent_swap = expect_block(
        lambda: shutdown.write_evidence_create_only(
            parent_fixture["output"],
            parent_fixture["manifest"],
            parent_document,
            before_open_hook=replace_parent_with_junction,
        ),
        code="shutdown_evidence_publication_outcome_unknown",
    )
    assert_true(
        (publication_backing / "shutdown-evidence.json").exists(),
        "owned outcome-unknown publication disappeared after ancestor swap",
    )
    return {
        "after_open_hardlink": hardlink,
        "after_close_name_swap": name_swap,
        "post_hash_exception": post_hash,
        "after_ownership_check_before_revoke_swap": late_swap,
        "ancestor_reparse_before_open": parent_swap,
        "replacement_preserved": True,
    }


def test_missing_order_duplicate_crash(base: pathlib.Path) -> dict[str, Any]:
    missing = _mutated_log_case(base, "missing", lambda records: records.pop())

    def disorder(records: list[dict[str, Any]]) -> None:
        records[-2]["event"], records[-1]["event"] = records[-1]["event"], records[-2]["event"]

    unordered = _mutated_log_case(base, "unordered", disorder)

    def duplicate(records: list[dict[str, Any]]) -> None:
        records.append(copy.deepcopy(records[-1]))

    duplicated = _mutated_log_case(base, "duplicate", duplicate)

    def crash(records: list[dict[str, Any]]) -> None:
        records[-2]["reason"] = "process_killed"

    killed = _mutated_log_case(base, "killed", crash)

    def untrusted(records: list[dict[str, Any]]) -> None:
        records[1]["event"] = "probe_error"

    untrusted = _mutated_log_case(base, "untrusted", untrusted)

    def append_newer_snapshot(records: list[dict[str, Any]]) -> None:
        newer = copy.deepcopy(records[3])
        newer["seq"] = 5
        newer["snapshot_seq"] = 3
        newer["snapshot_id"] = f"{newer['session_id']}:5"
        newer["ts"] = "2026-08-31T02:00:31.500000+00:00"
        records[-2]["seq"] = 6
        records[-1]["seq"] = 7
        records.insert(-2, newer)

    post_tip_snapshot = _mutated_log_case(
        base,
        "post-tip-snapshot",
        append_newer_snapshot,
        code="final_tip_snapshot_not_latest",
    )

    duplicate_snapshot_seq = _mutated_log_case(
        base,
        "duplicate-snapshot-seq",
        lambda records: records[3].__setitem__("snapshot_seq", 1),
        code="shutdown_snapshot_sequence_not_gap_free",
    )

    duplicate_record_seq = _mutated_log_case(
        base,
        "duplicate-record-seq",
        lambda records: records[3].__setitem__("seq", 3),
        code="shutdown_sequence_not_gap_free",
    )
    duplicate_snapshot_id = _mutated_log_case(
        base,
        "duplicate-snapshot-id",
        lambda records: records[3].__setitem__(
            "snapshot_id", records[1].get("snapshot_id")
        ),
        code="shutdown_snapshot_id_duplicate_or_invalid",
    )
    return {
        "missing": missing,
        "unordered": unordered,
        "duplicate": duplicated,
        "forced_kill": killed,
        "untrusted": untrusted,
        "post_tip_snapshot": post_tip_snapshot,
        "duplicate_snapshot_seq": duplicate_snapshot_seq,
        "duplicate_record_seq": duplicate_record_seq,
        "duplicate_snapshot_id": duplicate_snapshot_id,
        "terminal_only_tail_positive": True,
    }


def test_wrong_session_save_run_and_hash(base: pathlib.Path) -> dict[str, Any]:
    def wrong_session(records: list[dict[str, Any]]) -> None:
        records[-2]["session_id"] = "wrong-session"

    session = _mutated_log_case(base, "wrong-session", wrong_session)

    def wrong_save(records: list[dict[str, Any]]) -> None:
        records[-1]["save_id_hash"] = "sha256:" + "0" * 64

    save = _mutated_log_case(base, "wrong-save", wrong_save)

    run_fixture = make_fixture(base, "wrong-run")
    run = expect_block(
        lambda: shutdown.build_shutdown_evidence(
            manifest_path=run_fixture["manifest"],
            bridge_dir=run_fixture["bridge"],
            pair_report_path=run_fixture["report"],
            expected_pair_report_hash=run_fixture["report_hash"],
            expected_run_id="different-run",
            forward_step=run_fixture["forward_step"],
            inverse_step=run_fixture["inverse_step"],
            command_ids=run_fixture["command_ids"],
            command_bundle_hashes=run_fixture["bundle_hashes"],
            event_log=run_fixture["event_log"],
            startup_bundle_dir=run_fixture["startup_bundle"],
            expected_startup_evidence_hash=run_fixture["startup_evidence_hash"],
            expected_validation_process_id=run_fixture["validation_process_id"],
        )
    )
    unsafe_run_ids: dict[str, str] = {}
    for label, unsafe_run_id in {
        "traversal": "../escape",
        "reserved_device": "CON",
        "reserved_device_extension": "nul.fixture",
        "trailing_dot": "g0-unsafe.",
    }.items():
        unsafe_run_ids[label] = expect_block(
            lambda value=unsafe_run_id: shutdown.build_shutdown_evidence(
                manifest_path=run_fixture["manifest"],
                bridge_dir=run_fixture["bridge"],
                pair_report_path=run_fixture["report"],
                expected_pair_report_hash=run_fixture["report_hash"],
                expected_run_id=value,
                forward_step=run_fixture["forward_step"],
                inverse_step=run_fixture["inverse_step"],
                command_ids=run_fixture["command_ids"],
                command_bundle_hashes=run_fixture["bundle_hashes"],
                event_log=run_fixture["event_log"],
                startup_bundle_dir=run_fixture["startup_bundle"],
                expected_startup_evidence_hash=run_fixture["startup_evidence_hash"],
                expected_validation_process_id=run_fixture["validation_process_id"],
            ),
            code="run_id_invalid",
        )
    pair_hash_fixture = make_fixture(base, "wrong-pair-hash")
    pair_hash = expect_block(
        lambda: shutdown.build_shutdown_evidence(
            manifest_path=pair_hash_fixture["manifest"],
            bridge_dir=pair_hash_fixture["bridge"],
            pair_report_path=pair_hash_fixture["report"],
            expected_pair_report_hash="sha256:" + "0" * 64,
            expected_run_id=pair_hash_fixture["run_id"],
            forward_step=pair_hash_fixture["forward_step"],
            inverse_step=pair_hash_fixture["inverse_step"],
            command_ids=pair_hash_fixture["command_ids"],
            command_bundle_hashes=pair_hash_fixture["bundle_hashes"],
            event_log=pair_hash_fixture["event_log"],
            startup_bundle_dir=pair_hash_fixture["startup_bundle"],
            expected_startup_evidence_hash=pair_hash_fixture["startup_evidence_hash"],
            expected_validation_process_id=pair_hash_fixture["validation_process_id"],
        )
    )
    expected_hash_fixture = make_fixture(base, "wrong-evidence-hash")
    document = publish(expected_hash_fixture)
    expected_hash = expect_block(
        lambda: verify(expected_hash_fixture, "sha256:" + "0" * 64),
        code="shutdown_evidence_expected_hash_mismatch",
    )
    assert_true(document["evidence_hash"] != "sha256:" + "0" * 64, "hash fixture is degenerate")
    return {
        "wrong_session": session,
        "wrong_save": save,
        "wrong_run": run,
        "unsafe_run_ids": unsafe_run_ids,
        "wrong_pair_report_hash": pair_hash,
        "wrong_expected_evidence_hash": expected_hash,
    }


def test_startup_bundle_anchor_and_custody_refusals(base: pathlib.Path) -> dict[str, Any]:
    wrong_hash_fixture = make_fixture(base, "startup-wrong-hash")
    wrong_hash = dict(wrong_hash_fixture)
    wrong_hash["startup_evidence_hash"] = "sha256:" + "11" * 32
    hash_blocker = expect_block(
        lambda: build(wrong_hash), code="startup_evidence_expected_hash_mismatch"
    )

    wrong_pid_fixture = make_fixture(base, "startup-wrong-pid")
    wrong_pid = dict(wrong_pid_fixture)
    wrong_pid["validation_process_id"] = int(wrong_pid_fixture["validation_process_id"]) + 1
    pid_blocker = expect_block(
        lambda: build(wrong_pid), code="startup_validation_process_id_mismatch"
    )

    wrong_run_fixture = make_fixture(
        base,
        "startup-wrong-run",
        startup_run_id="certificate-run-does-not-match-lineage",
    )
    run_blocker = expect_block(
        lambda: build(wrong_run_fixture), code="startup_validation_run_lineage_mismatch"
    )

    wrong_session_fixture = make_fixture(
        base, "startup-bound-to-final-session", startup_session_stage="final"
    )
    session_blocker = expect_block(
        lambda: build(wrong_session_fixture), code="startup_session_lineage_mismatch"
    )

    wrong_path_fixture = make_fixture(base, "startup-wrong-path")
    escaped = dict(wrong_path_fixture)
    escaped["startup_bundle"] = (
        wrong_path_fixture["manifest"].parent / ".." / ".." / "escaped-startup-bundle"
    )
    path_blocker = expect_block(
        lambda: build(escaped), code="startup_bundle_path_not_exact"
    )

    extra_fixture = make_fixture(base, "startup-extra-entry")
    (extra_fixture["startup_bundle"] / "unexpected.json").write_text("{}\n", encoding="utf-8")
    extra_blocker = expect_block(
        lambda: build(extra_fixture), code="startup_bundle_entries_invalid"
    )

    exact_fixture = make_fixture(base, "startup-report-extra-field")
    exact_report_path = exact_fixture["startup_bundle"] / startup.REPORT_FILE
    make_file_writable(exact_report_path)
    exact_report = json.loads(exact_report_path.read_text(encoding="utf-8"))
    exact_report["unexpected_field"] = "attacker"
    unhashed = dict(exact_report)
    unhashed.pop("evidence_hash", None)
    exact_report["evidence_hash"] = canonical_hash(unhashed)
    write_json(exact_report_path, exact_report)
    exact_fixture["startup_evidence_hash"] = exact_report["evidence_hash"]
    exact_blocker = expect_block(
        lambda: build(exact_fixture), code="startup_bundle_replay_failed"
    )

    tamper_fixture = make_fixture(base, "startup-artifact-tamper")
    tamper_path = tamper_fixture["startup_bundle"] / startup.ARTIFACT_FILES["player_log_prefix"]
    make_file_writable(tamper_path)
    with tamper_path.open("ab") as handle:
        handle.write(b"[INFO] attacker\n")
    tamper_blocker = expect_block(
        lambda: build(tamper_fixture), code="startup_bundle_replay_failed"
    )

    prefix_tamper_fixture = make_fixture(base, "startup-prefix-tamper")
    prefix_tamper_path = (
        prefix_tamper_fixture["startup_bundle"]
        / startup.ARTIFACT_FILES["session_archive_prefix"]
    )
    make_file_writable(prefix_tamper_path)
    prefix_tamper_payload = prefix_tamper_path.read_bytes()
    assert_true(b'"save_dirty":false' in prefix_tamper_payload, "prefix tamper marker missing")
    prefix_tamper_path.write_bytes(
        prefix_tamper_payload.replace(b'"save_dirty":false', b'"save_dirty":true ', 1)
    )
    prefix_tamper_blocker = expect_block(
        lambda: build(prefix_tamper_fixture), code="startup_bundle_replay_failed"
    )

    prefix_truncate_fixture = make_fixture(base, "startup-prefix-truncate")
    prefix_truncate_path = (
        prefix_truncate_fixture["startup_bundle"]
        / startup.ARTIFACT_FILES["session_archive_prefix"]
    )
    make_file_writable(prefix_truncate_path)
    prefix_truncate_payload = prefix_truncate_path.read_bytes()
    prefix_truncate_path.write_bytes(prefix_truncate_payload[:-1])
    prefix_truncate_blocker = expect_block(
        lambda: build(prefix_truncate_fixture), code="startup_bundle_replay_failed"
    )

    hardlink_fixture = make_fixture(base, "startup-hardlink")
    hardlink_path = hardlink_fixture["startup_bundle"] / startup.ARTIFACT_FILES["consumed_prefs"]
    hardlink_payload = hardlink_path.read_bytes()
    make_file_writable(hardlink_path)
    hardlink_path.unlink()
    hardlink_source = hardlink_fixture["case"] / "hardlink-source.yaml"
    hardlink_source.write_bytes(hardlink_payload)
    os.link(hardlink_source, hardlink_path)
    hardlink_blocker = expect_block(
        lambda: build(hardlink_fixture), code="startup_bundle_replay_failed"
    )

    reparse_fixture = make_fixture(base, "startup-reparse")
    reparse_path = reparse_fixture["startup_bundle"]
    reparse_backing = reparse_fixture["case"] / "reparse-backing-bundle"
    os.rename(reparse_path, reparse_backing)
    junction = subprocess.run(
        [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(reparse_path),
            str(reparse_backing),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert_true(junction.returncode == 0, "TestRoot junction fixture could not be created")
    reparse_blocker = expect_block(
        lambda: build(reparse_fixture), code="evidence_path_contains_reparse"
    )

    toctou_fixture = make_fixture(base, "startup-toctou-name-swap")
    toctou_path = toctou_fixture["startup_bundle"] / startup.REPORT_FILE
    toctou_replacement = toctou_fixture["case"] / "same-content-report-replacement.json"
    toctou_replacement.write_bytes(toctou_path.read_bytes())

    def swap_after_first_startup_verify(_projection: Mapping[str, Any]) -> None:
        make_file_writable(toctou_path)
        os.replace(toctou_replacement, toctou_path)

    toctou_blocker = expect_block(
        lambda: build(
            toctou_fixture,
            after_startup_verify_hook=swap_after_first_startup_verify,
        ),
        code="startup_bundle_changed_across_validation",
    )

    log_toctou_fixture = make_fixture(base, "shutdown-log-toctou")

    def mutate_after_log_read(_source: Mapping[str, Any]) -> None:
        with log_toctou_fixture["event_log"].open("ab") as handle:
            handle.write(b"\n")

    log_toctou_blocker = expect_block(
        lambda: build(
            log_toctou_fixture,
            after_shutdown_log_read_hook=mutate_after_log_read,
        ),
        code="shutdown_log_changed_across_validation",
    )

    log_swap_fixture = make_fixture(base, "shutdown-log-name-swap")
    log_replacement = log_swap_fixture["case"] / "same-content-state.jsonl"
    log_replacement.write_bytes(log_swap_fixture["event_log"].read_bytes())

    def swap_log_after_read(_source: Mapping[str, Any]) -> None:
        os.replace(log_replacement, log_swap_fixture["event_log"])

    log_swap_blocker = expect_block(
        lambda: build(
            log_swap_fixture,
            after_shutdown_log_read_hook=swap_log_after_read,
        ),
        code="shutdown_log_changed_across_validation",
    )

    create_only_fixture = make_fixture(base, "startup-create-only")
    create_only_blocker = expect_startup_block(
        lambda: startup.publish_bundle(
            create_only_fixture["startup_bundle"],
            create_only_fixture["startup_captures"],
            create_only_fixture["startup_report"],
        ),
        code="bundle_already_exists",
    )
    replay = startup.verify_command(
        type("ReplayArgs", (), {"bundle_dir": str(create_only_fixture["startup_bundle"])})()
    )
    assert_true(
        replay["status"] == "passed"
        and replay["verified_evidence_hash"] == create_only_fixture["startup_evidence_hash"],
        "create-only refusal changed the startup bundle",
    )
    return {
        "wrong_external_evidence_hash": hash_blocker,
        "wrong_external_validation_process_id": pid_blocker,
        "wrong_validation_run_lineage": run_blocker,
        "wrong_session_lineage": session_blocker,
        "relative_escape_or_wrong_path": path_blocker,
        "duplicate_or_extra_bundle_entry": extra_blocker,
        "report_exact_fields": exact_blocker,
        "artifact_tamper": tamper_blocker,
        "startup_archive_prefix_byte_tamper": prefix_tamper_blocker,
        "startup_archive_prefix_truncation": prefix_truncate_blocker,
        "hardlink": hardlink_blocker,
        "reparse": reparse_blocker,
        "startup_bundle_cross_window_name_swap": toctou_blocker,
        "shutdown_log_cross_window_drift": log_toctou_blocker,
        "shutdown_log_same_content_name_swap": log_swap_blocker,
        "startup_bundle_create_only": create_only_blocker,
        "startup_bundle_replay_after_create_only_refusal": "passed",
    }


def test_rehashed_tamper_and_create_only(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "rehash-tamper")
    document = publish(fixture)
    tampered = copy.deepcopy(document)
    tampered["final_tip"]["world_id"] = 99
    tampered["evidence_hash"] = shutdown._hash_without(tampered, "evidence_hash")
    write_json(fixture["output"], tampered)
    blocker = expect_block(
        lambda: verify(fixture, tampered["evidence_hash"]),
        code="shutdown_evidence_reconstruction_mismatch",
    )

    create_only_fixture = make_fixture(base, "create-only")
    first = publish(create_only_fixture)
    before = file_sha256(create_only_fixture["output"])
    create_only = expect_block(
        lambda: shutdown.write_evidence_create_only(
            create_only_fixture["output"], create_only_fixture["manifest"], first
        ),
        code="shutdown_evidence_already_exists",
    )
    assert_true(file_sha256(create_only_fixture["output"]) == before, "create-only refusal changed publication")

    custody_fixture = make_fixture(base, "shutdown-evidence-custody")
    custody_document = publish(custody_fixture)
    custody_replacement = custody_fixture["case"] / "same-content-shutdown-evidence.json"
    custody_replacement.write_bytes(custody_fixture["output"].read_bytes())

    def swap_after_evidence_snapshot(_document: Mapping[str, Any]) -> None:
        os.replace(custody_replacement, custody_fixture["output"])

    custody = expect_block(
        lambda: verify(
            custody_fixture,
            custody_document["evidence_hash"],
            after_evidence_snapshot_hook=swap_after_evidence_snapshot,
        ),
        code="shutdown_evidence_changed_across_reconstruction",
    )
    return {
        "rehashed_cross_binding_tamper": blocker,
        "create_only_republication": create_only,
        "shutdown_evidence_same_content_name_swap": custody,
    }


def test_source_changed_after_publication(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "source-changed")
    document = publish(fixture)
    with fixture["event_log"].open("ab") as handle:
        handle.write(b"\n")
    blocker = expect_block(lambda: verify(fixture, document["evidence_hash"]))
    return {"source_file_rehash": blocker, "process_zero_is_not_an_input": True}


def test_syntax_and_parser(base: pathlib.Path) -> dict[str, Any]:
    del base
    targets = [ROOT / "scripts" / "create_shutdown_evidence.py", pathlib.Path(__file__).resolve()]
    for target in targets:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as handle:
            compiled = pathlib.Path(handle.name)
        try:
            py_compile.compile(str(target), cfile=str(compiled), doraise=True)
        finally:
            compiled.unlink(missing_ok=True)
    parser = shutdown.build_parser()
    operation_parsers = next(
        action for action in parser._actions if getattr(action, "choices", None)
    ).choices
    verify_parser = operation_parsers["verify"]
    create_parser = operation_parsers["create"]
    verify_required = {action.dest for action in verify_parser._actions if action.required}
    create_required = {action.dest for action in create_parser._actions if action.required}
    startup_anchors = {
        "startup_consumption_bundle",
        "startup_consumption_evidence_hash",
        "validation_process_id",
    }
    assert_true(
        {"shutdown_evidence", "shutdown_evidence_hash", *startup_anchors}.issubset(
            verify_required
        )
        and {"event_log", "output", *startup_anchors}.issubset(create_required),
        "create/verify parsers do not require all startup and shutdown trust anchors",
    )
    return {
        "py_compile": "passed",
        "create_required_cli_anchors": sorted(create_required),
        "verify_required_cli_anchors": sorted(verify_required),
    }


def main() -> int:
    global ASSERTION_COUNT
    ASSERTION_COUNT = 0
    tests: list[tuple[str, Callable[[pathlib.Path], dict[str, Any]]]] = [
        ("syntax_and_required_cli_anchors", test_syntax_and_parser),
        ("archive_and_root_log_positive", test_positive),
        ("runtime_identity_and_trusted_start_refusals", test_runtime_identity_and_start_envelope),
        ("startup_record_binding_refusals", test_startup_record_binding_refusals),
        (
            "origin_suffix_and_activation_reload_refusals",
            test_origin_suffix_and_activation_reload_refusals,
        ),
        ("cross_session_lineage_refusals", test_cross_session_lineage_refusals),
        ("origin_archive_custody_refusals", test_origin_archive_custody_refusals),
        ("startup_bundle_anchor_and_custody_refusals", test_startup_bundle_anchor_and_custody_refusals),
        ("bridge_relative_path_contract_refusals", test_bridge_relative_path_contract),
        ("descriptor_identity_hardlink_name_swap_refusals", test_descriptor_identity_and_link_refusals),
        ("create_only_publication_ownership_refusals", test_create_only_publication_ownership),
        ("missing_unordered_duplicate_crash_untrusted_refusals", test_missing_order_duplicate_crash),
        ("wrong_session_save_run_hash_refusals", test_wrong_session_save_run_and_hash),
        ("rehashed_tamper_and_create_only_refusals", test_rehashed_tamper_and_create_only),
        ("source_file_changed_after_publication_refusal", test_source_changed_after_publication),
    ]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="oni-shutdown-evidence-") as temporary:
        base = pathlib.Path(temporary).resolve()
        for name, test in tests:
            try:
                results.append({"name": name, "status": "passed", "evidence": test(base)})
            except BaseException as exc:
                results.append({"name": name, "status": "failed", "error": type(exc).__name__})
    passed = sum(item["status"] == "passed" for item in results)
    payload = {
        "schema": "oni.normal-shutdown-evidence-verification.v3",
        "status": "passed" if passed == len(results) else "failed",
        "validation_level": "offline_contract_and_adversarial_fixture",
        "scope": "temporary_TestRoot_only",
        "live_oni_accessed": False,
        "game_or_bridge_launched": False,
        "test_count": len(results),
        "assertion_count": ASSERTION_COUNT,
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "scripts": {
            "create_shutdown_evidence.py": file_sha256(ROOT / "scripts" / "create_shutdown_evidence.py"),
            "verify_shutdown_evidence.py": file_sha256(pathlib.Path(__file__).resolve()),
        },
        "tests": results,
    }
    payload["report_hash"] = canonical_hash(payload)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = REPORT.with_name(f".{REPORT.name}.tmp")
    write_json(temporary_report, payload)
    temporary_report.replace(REPORT)
    print(json.dumps({"status": payload["status"], "passed": passed, "total": len(results)}))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
