#!/usr/bin/env python3
"""Adversarial offline verification for startup-consumption evidence."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
from dataclasses import replace
from typing import Any, Callable


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import create_startup_consumption_evidence as core


ASSERTIONS = 0


def check(condition: bool, message: str) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(message)


def json_payload(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(string_values(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(string_values(item))
        return result
    return []


def capture(payload: bytes) -> core.Capture:
    return core.Capture(payload, core.sha256_bytes(payload), len(payload), "sha256:" + "ab" * 32)


def rehash_document(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["document_hash"] = core.json_document_hash(result)
    return result


def contract_hash(value: Any) -> str:
    return core.sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def seal(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result[field] = contract_hash(result)
    return result


def make_runtime_bootstrap_result(
    *,
    run_id: str,
    environment_manifest_sha256: str,
    expected_mod_version: str,
    expected_probe_dll_sha256: str,
) -> dict[str, Any]:
    transaction_id = "rtb-" + "12" * 12
    plan_hash = "sha256:" + "13" * 32
    artifacts = {
        "OniAiProbeMod.dll": {"length": 101, "sha256": expected_probe_dll_sha256},
        "OniAiProbeMod.pdb": {"length": 102, "sha256": "sha256:" + "14" * 32},
        "mod.yaml": {"length": 103, "sha256": "sha256:" + "15" * 32},
        "mod_info.yaml": {"length": 104, "sha256": "sha256:" + "16" * 32},
    }
    tree_content = {
        "exists": True,
        "entries": [
            {
                "kind": "file",
                "relative": name,
                "length": artifacts[name]["length"],
                "sha256": artifacts[name]["sha256"],
            }
            for name in sorted(artifacts, key=str.casefold)
        ],
    }
    tree_hash = contract_hash(tree_content)
    commit = {
        "schema": "oni.live-runtime-bootstrap-journal-entry.v1",
        "index": 9,
        "transaction_id": transaction_id,
        "plan_hash": plan_hash,
        "event": "commit_verified",
        "data": {
            "probe_tree_content_hash": tree_hash,
            "bridge_tree_content_hash": "sha256:" + "17" * 32,
            "archive_relative": None,
            "old_probe_recovery": None,
        },
        "previous_entry_hash": "sha256:" + "18" * 32,
    }

    def lock_projection(epoch: int, purpose: str, fill: str) -> tuple[dict[str, Any], dict[str, Any]]:
        claim = seal(
            {
                "schema": "oni.live-runtime-bootstrap-environment-lock-claim.v1",
                "transaction_id": transaction_id,
                "plan_hash": plan_hash,
                "epoch": epoch,
                "purpose": purpose,
                "intent_hash": "sha256:" + fill * 64,
                "payload_sha256": "sha256:" + chr(ord(fill) + 1) * 64,
                "lock_identity": [1, epoch + 100, 32768],
            },
            "claim_hash",
        )
        release = seal(
            {
                "schema": "oni.live-runtime-bootstrap-environment-lock-release.v1",
                "transaction_id": transaction_id,
                "plan_hash": plan_hash,
                "epoch": epoch,
                "purpose": purpose,
                "intent_hash": claim["intent_hash"],
                "claim_hash": claim["claim_hash"],
                "released_identity": claim["lock_identity"],
                "payload_sha256": claim["payload_sha256"],
            },
            "release_hash",
        )
        return claim, release

    install_claim, install_release = lock_projection(0, "install", "2")
    ready_claim, ready_release = lock_projection(1, "replay", "4")
    document: dict[str, Any] = {
        "schema": "oni.live-runtime-bootstrap-result.v1",
        "status": "installed_ready",
        "classification": "static_probe",
        "run_id": run_id,
        "transaction_id": transaction_id,
        "plan_hash": plan_hash,
        "environment_manifest_sha256": environment_manifest_sha256,
        "release_dll_sha256": expected_probe_dll_sha256,
        "expected_mod_version": expected_mod_version,
        "probe_tree_content_hash": tree_hash,
        "installed_probe_artifacts": artifacts,
        "installed_probe_tree_identity": {
            "exists": True,
            "root_identity": [1, 99, 16384],
            "entries": [
                {"relative": name, "kind": "file", "identity": [1, 200 + index, 32768]}
                for index, name in enumerate(sorted(artifacts, key=str.casefold))
            ],
        },
        "bridge_tree_content_hash": commit["data"]["bridge_tree_content_hash"],
        "bootstrap_document_hash": "sha256:" + "19" * 32,
        "bootstrap_file_sha256": "sha256:" + "1a" * 32,
        "archive_relative": None,
        "old_probe_recovery_relative": None,
        "install_commit_entry": commit,
        "install_commit_entry_hash": contract_hash(commit),
        "install_stage_sha256": "sha256:" + "1b" * 32,
        "install_stage_hash": "sha256:" + "1c" * 32,
        "producer": {"length": 5000, "sha256": "sha256:" + "1d" * 32},
        "install_lock_epoch": install_claim["epoch"],
        "install_lock_claim": install_claim,
        "install_lock_claim_hash": install_claim["claim_hash"],
        "install_lock_release": install_release,
        "install_lock_release_hash": install_release["release_hash"],
        "ready_lock_epoch": ready_claim["epoch"],
        "ready_lock_claim": ready_claim,
        "ready_lock_claim_hash": ready_claim["claim_hash"],
        "ready_lock_release": ready_release,
        "ready_lock_release_hash": ready_release["release_hash"],
        "candidate_sha256": "sha256:" + "1e" * 32,
        "candidate_hash": "sha256:" + "1f" * 32,
        "physical_commit_complete": True,
        "shared_lock_released": True,
        "game_launched": False,
    }
    document["result_hash"] = contract_hash(document)
    raw = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    return {
        "path_id_lower": "sha256:" + "20" * 32,
        "external_expected_sha256": core.sha256_bytes(raw),
        "raw_sha256": core.sha256_bytes(raw),
        "raw_byte_length": len(raw),
        "raw_utf8_base64": base64.b64encode(raw).decode("ascii"),
        "result_hash": document["result_hash"],
        "transaction_id": transaction_id,
        "probe_tree_content_hash": tree_hash,
        "document": document,
    }


def common_fields(binding: dict[str, Any], process_id: int, session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "game_build": binding["expected_game_build"],
        "mod_version": binding["expected_mod_version"],
        "save_id_hash": binding["requested_loader_save_sha256"],
        "save_dirty": False,
        "world_id": 0,
        "validation_run_id": binding["validation_run_id"],
        "startup_binding_hash": None,
        "startup_intent_hash": binding["startup_intent_hash"],
        "runtime_config_evidence_sha256": binding["runtime_config_evidence_sha256"],
        "validation_process_id": process_id,
        "startup_binding_status": "verified",
        "requested_save_path_id": binding["requested_loader_path_id_upper"],
        "active_save_path_id": binding["requested_loader_path_id_upper"],
    }


def make_material(root: pathlib.Path, process_id: int) -> dict[str, Any]:
    loader = root / "Klei" / "OxygenNotIncluded" / "cloud_save_files" / "test" / "启动测试.sav"
    loader.parent.mkdir(parents=True, exist_ok=True)
    loader_text = str(loader)
    loader_path_id = core.path_id_upper(loader_text)
    save_hash = "sha256:" + "66" * 32
    environment_manifest_sha256 = "sha256:" + "33" * 32
    expected_probe_dll_sha256 = "sha256:" + "55" * 32
    runtime_bootstrap_result = make_runtime_bootstrap_result(
        run_id="startup-evidence-test-01",
        environment_manifest_sha256=environment_manifest_sha256,
        expected_mod_version="0.7.0",
        expected_probe_dll_sha256=expected_probe_dll_sha256,
    )
    intent: dict[str, Any] = {
        "schema": core.INTENT_SCHEMA,
        "validation_run_id": "startup-evidence-test-01",
        "startup_nonce": "startup-0123456789abcdef0123456789abcdef",
        "environment_manifest_sha256": environment_manifest_sha256,
        "requested_loader_path_id_lower": "sha256:" + "44" * 32,
        "requested_loader_path_id_upper": loader_path_id,
        "requested_loader_save_sha256": save_hash,
        "requested_loader_save_length": 987654,
        "expected_game_build": "cl-744825",
        "expected_mod_version": "0.7.0",
        "expected_probe_dll_sha256": expected_probe_dll_sha256,
        "runtime_bootstrap_result_path_id_lower": runtime_bootstrap_result[
            "path_id_lower"
        ],
        "runtime_bootstrap_result_sha256": runtime_bootstrap_result[
            "external_expected_sha256"
        ],
        "runtime_bootstrap_result_hash": runtime_bootstrap_result["result_hash"],
        "runtime_bootstrap_transaction_id": runtime_bootstrap_result[
            "transaction_id"
        ],
        "installed_probe_tree_content_hash": runtime_bootstrap_result[
            "probe_tree_content_hash"
        ],
        "auto_resume_semantics": "one_shot_nonempty_exact_loader",
    }
    intent["document_hash"] = core.json_document_hash(intent)
    runtime_evidence = {
        "schema": "oni.live-validation-runtime-config.v5",
        "run_id": intent["validation_run_id"],
        "mode": "apply",
        "configured_at": "2026-08-31T12:00:01+08:00",
        "transaction_id": "config-test-transaction",
        "environment_manifest_sha256": intent["environment_manifest_sha256"],
        "bindings": {},
        "runtime": {
            "bootstrap": {},
            "runtime_bootstrap_result": runtime_bootstrap_result,
            "release_dll": {},
            "installed_probe": {},
            "initial_disabled_policy": {},
            "mod_version": intent["expected_mod_version"],
            "probe_static_id": "oni-ai-colony-agent",
            "dll_four_way_identical": True,
        },
        "configs": {},
        "loader_policy": {},
        "startup_intent": intent,
        "mod_policy": {},
        "restoration": {},
    }
    runtime_evidence["document_hash"] = core.json_document_hash(runtime_evidence)
    runtime_payload = json_payload(runtime_evidence)
    runtime_hash = core.sha256_bytes(runtime_payload)
    binding: dict[str, Any] = {
        "schema": core.BINDING_SCHEMA,
        "validation_run_id": intent["validation_run_id"],
        "startup_nonce": intent["startup_nonce"],
        "startup_intent_hash": intent["document_hash"],
        "runtime_config_evidence_sha256": runtime_hash,
        "environment_manifest_sha256": intent["environment_manifest_sha256"],
        "expected_game_build": intent["expected_game_build"],
        "expected_mod_version": intent["expected_mod_version"],
        "expected_probe_dll_sha256": intent["expected_probe_dll_sha256"],
        "runtime_bootstrap_result_path_id_lower": intent[
            "runtime_bootstrap_result_path_id_lower"
        ],
        "runtime_bootstrap_result_sha256": intent[
            "runtime_bootstrap_result_sha256"
        ],
        "runtime_bootstrap_result_hash": intent["runtime_bootstrap_result_hash"],
        "runtime_bootstrap_transaction_id": intent[
            "runtime_bootstrap_transaction_id"
        ],
        "installed_probe_tree_content_hash": intent[
            "installed_probe_tree_content_hash"
        ],
        "requested_loader_path_id_upper": loader_path_id,
        "requested_loader_save_sha256": save_hash,
        "requested_loader_save_length": intent["requested_loader_save_length"],
        "created_at": runtime_evidence["configured_at"],
    }
    binding["document_hash"] = core.json_document_hash(binding)
    binding_payload = json_payload(binding)
    binding_hash = core.sha256_bytes(binding_payload)

    prefs_payload = (
        "strings:\r\n"
        "  InstalledLanguage: zh_klei\r\n"
        f"  SaveFilenameKey/: {loader_text}\r\n"
        "ints:\r\n"
        "  SESSION_ID: 1\r\n"
    ).encode("utf-8")
    player_payload = (
        "[INFO] boot\n"
        f"[INFO] Loading save file: {loader_text}\n"
        "[INFO] loading world\n"
        f"[INFO] Loaded [{loader_text}]\n"
        "[ONI AI Probe] STARTUP_BINDING_VERIFIED "
        f"validation_run_id={binding['validation_run_id']} "
        f"startup_binding_hash={binding_hash} "
        f"startup_intent_hash={binding['startup_intent_hash']} "
        f"validation_process_id={process_id} "
        f"requested_save_path_id={loader_path_id} "
        f"active_save_path_id={loader_path_id}\n"
    ).encode("utf-8")

    session_id = "session-20260831-120001-deadbeef"
    derivative = loader.with_name("activation-derivative.sav")
    derivative_path_id = core.path_id_upper(str(derivative))
    derivative_save_hash = "sha256:" + "77" * 32
    common = common_fields(binding, process_id, session_id)
    common["startup_binding_hash"] = binding_hash
    prefix_records = [
        {
            **common,
            "schema": "oni.event.v2",
            "event": "session_started",
            "reason": "save_loader_load_complete",
            "seq": 1,
            "ts": "2026-08-31T04:00:01.000000Z",
        },
        {
            **common,
            "schema": "oni.event.v2",
            "event": "save_loaded",
            "reason": "save_loader_load_complete",
            "seq": 2,
            "ts": "2026-08-31T04:00:01.010000Z",
        },
        {
            **common,
            "schema": "oni.state.v2",
            "event": "state_snapshot",
            "seq": 3,
            "snapshot_seq": 1,
            "snapshot_id": session_id + ":3",
            "ts": "2026-08-31T04:00:02.000000Z",
        },
    ]
    save_started_record = {
        **common,
        "schema": "oni.event.v2",
        "event": "save_write_started",
        "seq": 4,
        "ts": "2026-08-31T04:00:02.500000Z",
        "is_auto_save": False,
        "update_save_pointer": True,
        "requested_save_path_id": derivative_path_id,
        "before_save_id_hash": save_hash,
        "save_dirty": True,
    }
    save_record = {
        **common,
        "schema": "oni.event.v2",
        "event": "save_write_completed",
        "seq": 5,
        "ts": "2026-08-31T04:00:03.000000Z",
        "success": True,
        "is_auto_save": False,
        "update_save_pointer": True,
        "file_rewrite_observed": True,
        "save_file_readable": True,
        "requested_save_path_id": derivative_path_id,
        "result_save_path_id": derivative_path_id,
        "active_save_path_id": derivative_path_id,
        "before_save_id_hash": save_hash,
        "written_save_id_hash": derivative_save_hash,
        "active_save_id_hash": derivative_save_hash,
        "save_id_hash": derivative_save_hash,
    }
    unloaded_record = {
        **common,
        "schema": "oni.event.v2",
        "event": "save_unloaded",
        "reason": "save_loader_load_start",
        "seq": 6,
        "ts": "2026-08-31T04:00:03.500000Z",
        "active_save_path_id": derivative_path_id,
        "save_id_hash": derivative_save_hash,
    }
    ended_record = {
        **common,
        "schema": "oni.event.v2",
        "event": "session_ended",
        "reason": "save_loader_load_start",
        "seq": 7,
        "ts": "2026-08-31T04:00:04.000000Z",
        "active_save_path_id": derivative_path_id,
        "save_id_hash": derivative_save_hash,
    }
    records = [
        *prefix_records,
        save_started_record,
        save_record,
        unloaded_record,
        ended_record,
    ]
    prefix_payload = archive_payload(prefix_records)
    full_archive_payload = archive_payload(records)
    captures = {
        "runtime_config_evidence": capture(runtime_payload),
        "startup_binding": capture(binding_payload),
        "consumed_prefs": capture(prefs_payload),
        "player_log_prefix": capture(player_payload),
        "session_archive_prefix": capture(prefix_payload),
    }
    return {
        "loader": loader_text,
        "intent": intent,
        "runtime_evidence": runtime_evidence,
        "runtime_bootstrap_result": runtime_bootstrap_result,
        "runtime_hash": runtime_hash,
        "binding": binding,
        "binding_hash": binding_hash,
        "prefix_records": prefix_records,
        "records": records,
        "full_archive_payload": full_archive_payload,
        "captures": captures,
        "session_id": session_id,
        "derivative_path_id": derivative_path_id,
        "derivative_save_hash": derivative_save_hash,
    }


def expect_code(action: Callable[[], Any], code: str) -> None:
    try:
        action()
    except core.StartupEvidenceError as exc:
        check(exc.code == code, f"expected {code}, observed {exc.code}")
    else:
        raise AssertionError(f"expected failure {code}")


def with_capture(material: dict[str, Any], key: str, payload: bytes) -> dict[str, core.Capture]:
    captures = dict(material["captures"])
    captures[key] = capture(payload)
    return captures


def validate(material: dict[str, Any], captures: dict[str, core.Capture] | None = None) -> core.ValidatedMaterial:
    return core.validate_material(
        captures or material["captures"],
        expected_runtime_sha256=material["runtime_hash"],
        expected_binding_sha256=material["binding_hash"],
        process_id=os.getpid(),
    )


def reseal_runtime_bootstrap_chain(
    material: dict[str, Any],
    mutate: Callable[[dict[str, Any]], None],
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Coherently re-sign every local JSON layer after a result mutation."""

    record = copy.deepcopy(material["runtime_bootstrap_result"])
    document = record["document"]
    mutate(document)
    result_material = dict(document)
    result_material.pop("result_hash", None)
    document["result_hash"] = contract_hash(result_material)
    raw = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    record.update(
        {
            "external_expected_sha256": core.sha256_bytes(raw),
            "raw_sha256": core.sha256_bytes(raw),
            "raw_byte_length": len(raw),
            "raw_utf8_base64": base64.b64encode(raw).decode("ascii"),
            "result_hash": document["result_hash"],
            "transaction_id": document["transaction_id"],
            "probe_tree_content_hash": document["probe_tree_content_hash"],
            "document": document,
        }
    )
    evidence = copy.deepcopy(material["runtime_evidence"])
    evidence["runtime"]["runtime_bootstrap_result"] = record
    intent = evidence["startup_intent"]
    intent.update(
        {
            "runtime_bootstrap_result_path_id_lower": record["path_id_lower"],
            "runtime_bootstrap_result_sha256": record["external_expected_sha256"],
            "runtime_bootstrap_result_hash": record["result_hash"],
            "runtime_bootstrap_transaction_id": record["transaction_id"],
            "installed_probe_tree_content_hash": record["probe_tree_content_hash"],
        }
    )
    evidence["startup_intent"] = rehash_document(intent)
    evidence = rehash_document(evidence)
    runtime_payload = json_payload(evidence)
    binding = copy.deepcopy(material["binding"])
    binding.update(
        {
            "runtime_config_evidence_sha256": core.sha256_bytes(runtime_payload),
            "startup_intent_hash": evidence["startup_intent"]["document_hash"],
            "runtime_bootstrap_result_path_id_lower": record["path_id_lower"],
            "runtime_bootstrap_result_sha256": record["external_expected_sha256"],
            "runtime_bootstrap_result_hash": record["result_hash"],
            "runtime_bootstrap_transaction_id": record["transaction_id"],
            "installed_probe_tree_content_hash": record["probe_tree_content_hash"],
        }
    )
    binding = rehash_document(binding)
    return runtime_payload, json_payload(binding), binding


def test_positive(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root, os.getpid())
    result = validate(material)
    check(result.session_id == material["session_id"], "session id did not survive validation")
    check(result.record_count == 3 and result.snapshot_count == 1, "archive counts are wrong")
    check(result.prefs.auto_resume_count == 0, "AutoResume was not proven consumed")
    check(result.player.loading_line < result.player.loaded_line < result.player.marker_line, "log order is wrong")
    return {"material": "passed"}


def test_binding_and_intent_tamper(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root, os.getpid())
    binding = copy.deepcopy(material["binding"])
    binding["requested_loader_save_length"] += 1
    binding = rehash_document(binding)
    expect_code(
        lambda: core.validate_binding(
            json_payload(binding), material["binding_hash"], material["runtime_hash"]
        ),
        "binding_hash_mismatch",
    )

    intent = copy.deepcopy(material["intent"])
    intent["expected_game_build"] = "cl-1"
    intent = rehash_document(intent)
    evidence = copy.deepcopy(material["runtime_evidence"])
    evidence["startup_intent"] = intent
    evidence = rehash_document(evidence)
    runtime_payload = json_payload(evidence)
    binding = copy.deepcopy(material["binding"])
    binding["runtime_config_evidence_sha256"] = core.sha256_bytes(runtime_payload)
    binding["startup_intent_hash"] = intent["document_hash"]
    binding = rehash_document(binding)
    expect_code(
        lambda: core.validate_runtime_evidence(
            runtime_payload, core.sha256_bytes(runtime_payload), binding
        ),
        "startup_intent_game_build_mismatch",
    )

    def runtime_case(
        evidence_value: dict[str, Any],
        expected_code: str,
        *,
        rehash: bool = True,
    ) -> None:
        candidate = rehash_document(evidence_value) if rehash else evidence_value
        candidate_payload = json_payload(candidate)
        candidate_binding = copy.deepcopy(material["binding"])
        candidate_binding["runtime_config_evidence_sha256"] = core.sha256_bytes(candidate_payload)
        candidate_binding = rehash_document(candidate_binding)
        expect_code(
            lambda: core.validate_runtime_evidence(
                candidate_payload,
                core.sha256_bytes(candidate_payload),
                candidate_binding,
            ),
            expected_code,
        )

    extra_evidence = copy.deepcopy(material["runtime_evidence"])
    extra_evidence["unexpected"] = True
    runtime_case(extra_evidence, "runtime_evidence_fields_invalid")
    bad_evidence_hash = copy.deepcopy(material["runtime_evidence"])
    bad_evidence_hash["mode"] = "changed-without-rehash"
    runtime_case(bad_evidence_hash, "runtime_evidence_document_hash_mismatch", rehash=False)
    extra_intent = copy.deepcopy(material["runtime_evidence"])
    extra_intent["startup_intent"]["unexpected"] = True
    extra_intent["startup_intent"] = rehash_document(extra_intent["startup_intent"])
    runtime_case(extra_intent, "startup_intent_fields_invalid")
    bad_semantics = copy.deepcopy(material["runtime_evidence"])
    bad_semantics["startup_intent"]["auto_resume_semantics"] = "reusable"
    bad_semantics["startup_intent"] = rehash_document(bad_semantics["startup_intent"])
    runtime_case(bad_semantics, "startup_intent_auto_resume_semantics_invalid")
    wrong_time = copy.deepcopy(material["runtime_evidence"])
    wrong_time["configured_at"] = "2026-08-31T12:00:02+08:00"
    runtime_case(wrong_time, "runtime_evidence_created_at_mismatch")

    duplicate = material["captures"]["startup_binding"].payload.replace(
        b'"schema":"oni.runtime-startup-binding.v2",',
        b'"schema":"oni.runtime-startup-binding.v2","schema":"duplicate",',
        1,
    )
    expect_code(
        lambda: core.validate_binding(
            duplicate, core.sha256_bytes(duplicate), material["runtime_hash"]
        ),
        "json_invalid",
    )
    return {"binding_intent_tamper": "blocked"}


def test_v2_v5_bootstrap_contract_refusals(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root, os.getpid())
    added_fields = {
        "runtime_bootstrap_result_path_id_lower",
        "runtime_bootstrap_result_sha256",
        "runtime_bootstrap_result_hash",
        "runtime_bootstrap_transaction_id",
        "installed_probe_tree_content_hash",
    }

    legacy_binding = copy.deepcopy(material["binding"])
    legacy_binding["schema"] = "oni.runtime-startup-binding.v1"
    legacy_binding = rehash_document(legacy_binding)
    legacy_binding_payload = json_payload(legacy_binding)
    expect_code(
        lambda: core.validate_binding(
            legacy_binding_payload,
            core.sha256_bytes(legacy_binding_payload),
            material["runtime_hash"],
        ),
        "binding_schema_invalid",
    )
    for field in sorted(added_fields):
        missing_binding = copy.deepcopy(material["binding"])
        del missing_binding[field]
        missing_binding = rehash_document(missing_binding)
        missing_binding_payload = json_payload(missing_binding)
        expect_code(
            lambda payload=missing_binding_payload: core.validate_binding(
                payload, core.sha256_bytes(payload), material["runtime_hash"]
            ),
            "binding_fields_invalid",
        )

    bool_binding = copy.deepcopy(material["binding"])
    bool_binding["runtime_bootstrap_result_hash"] = True
    bool_binding = rehash_document(bool_binding)
    bool_binding_payload = json_payload(bool_binding)
    expect_code(
        lambda: core.validate_binding(
            bool_binding_payload,
            core.sha256_bytes(bool_binding_payload),
            material["runtime_hash"],
        ),
        "hash_invalid",
    )

    def runtime_case(evidence: dict[str, Any], expected_code: str) -> None:
        candidate = rehash_document(evidence)
        payload = json_payload(candidate)
        binding = copy.deepcopy(material["binding"])
        binding["runtime_config_evidence_sha256"] = core.sha256_bytes(payload)
        if type(candidate.get("startup_intent")) is dict:
            binding["startup_intent_hash"] = candidate["startup_intent"].get(
                "document_hash"
            )
        expect_code(
            lambda: core.validate_runtime_evidence(
                payload, core.sha256_bytes(payload), binding
            ),
            expected_code,
        )

    legacy_runtime = copy.deepcopy(material["runtime_evidence"])
    legacy_runtime["schema"] = "oni.live-validation-runtime-config.v4"
    runtime_case(legacy_runtime, "runtime_evidence_schema_invalid")
    legacy_intent = copy.deepcopy(material["runtime_evidence"])
    legacy_intent["startup_intent"]["schema"] = "oni.live-validation-startup-intent.v1"
    legacy_intent["startup_intent"] = rehash_document(legacy_intent["startup_intent"])
    runtime_case(legacy_intent, "startup_intent_schema_invalid")
    for field in sorted(added_fields):
        missing_intent = copy.deepcopy(material["runtime_evidence"])
        del missing_intent["startup_intent"][field]
        missing_intent["startup_intent"] = rehash_document(
            missing_intent["startup_intent"]
        )
        runtime_case(missing_intent, "startup_intent_fields_invalid")

    missing_record = copy.deepcopy(material["runtime_evidence"])
    del missing_record["runtime"]["runtime_bootstrap_result"]["raw_utf8_base64"]
    runtime_case(missing_record, "runtime_bootstrap_result_record_fields_invalid")
    bool_record = copy.deepcopy(material["runtime_evidence"])
    bool_record["runtime"]["runtime_bootstrap_result"]["raw_byte_length"] = True
    runtime_case(bool_record, "runtime_bootstrap_result_raw_fields_invalid")

    for label, mutate, expected_code in (
        (
            "physical_commit_bool_int",
            lambda document: document.__setitem__("physical_commit_complete", 1),
            "runtime_bootstrap_result_status_invalid",
        ),
        (
            "lock_epoch_bool_int",
            lambda document: document.__setitem__("install_lock_epoch", True),
            "runtime_bootstrap_result_lock_epoch_invalid",
        ),
        (
            "artifact_length_bool_int",
            lambda document: document["installed_probe_artifacts"][
                "OniAiProbeMod.pdb"
            ].__setitem__("length", True),
            "runtime_bootstrap_result_artifact_invalid",
        ),
    ):
        runtime_payload, _binding_payload, binding = reseal_runtime_bootstrap_chain(
            material, mutate
        )
        expect_code(
            lambda payload=runtime_payload, candidate=binding: core.validate_runtime_evidence(
                payload, core.sha256_bytes(payload), candidate
            ),
            expected_code,
        )

    resigned_runtime, resigned_binding, _binding = reseal_runtime_bootstrap_chain(
        material,
        lambda document: document.__setitem__(
            "expected_mod_version", "0.7.0-resigned"
        ),
    )
    resigned_captures = with_capture(
        material, "runtime_config_evidence", resigned_runtime
    )
    resigned_captures["startup_binding"] = capture(resigned_binding)
    expect_code(
        lambda: validate(material, resigned_captures),
        "binding_hash_mismatch",
    )
    return {
        "legacy_v1_v4": "blocked",
        "missing_fields": "blocked",
        "bool_int_resigned": "blocked",
        "external_anchor_resigned_tamper": "blocked",
    }


def test_prefs_refusals(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root, os.getpid())
    expected = material["binding"]["requested_loader_path_id_upper"]
    base = material["captures"]["consumed_prefs"].payload
    auto = base.replace(b"ints:\r\n", f"  AutoResumeSaveFile: {material['loader']}\r\nints:\r\n".encode("utf-8"))
    expect_code(lambda: core.validate_consumed_prefs(auto, expected), "prefs_auto_resume_not_consumed")
    missing = base.replace(b"SaveFilenameKey/", b"FormerSaveFilenameKey/")
    expect_code(lambda: core.validate_consumed_prefs(missing, expected), "prefs_target_key_ambiguous")
    duplicate = base.replace(
        b"ints:\r\n",
        f"  SaveFilenameKey/: {material['loader']}\r\nints:\r\n".encode("utf-8"),
    )
    expect_code(lambda: core.validate_consumed_prefs(duplicate, expected), "prefs_save_filename_count_invalid")
    wrong = base.replace(str(material["loader"]).encode("utf-8"), str(root / "wrong.sav").encode("utf-8"))
    expect_code(lambda: core.validate_consumed_prefs(wrong, expected), "prefs_loader_path_mismatch")
    nested = base.replace(b"  SaveFilenameKey/:", b"    SaveFilenameKey/:")
    expect_code(lambda: core.validate_consumed_prefs(nested, expected), "prefs_target_key_not_direct")
    escaped_duplicate = base + (
        '"str\\u0069ngs":\r\n'
        f'  "SaveFilenameKey\\u002f": {root / "semantic-last-wins.sav"}\r\n'
    ).encode("utf-8")
    expect_code(
        lambda: core.validate_consumed_prefs(escaped_duplicate, expected),
        "prefs_yaml_ambiguous",
    )
    quoted_key = base.replace(b"SaveFilenameKey/", b'"SaveFilenameKey/"')
    expect_code(lambda: core.validate_consumed_prefs(quoted_key, expected), "prefs_yaml_ambiguous")
    merge_key = base.replace(
        b"  InstalledLanguage: zh_klei\r\n",
        b"  <<: *defaults\r\n  InstalledLanguage: zh_klei\r\n",
    )
    expect_code(lambda: core.validate_consumed_prefs(merge_key, expected), "prefs_yaml_ambiguous")
    flow_value = base.replace(b"InstalledLanguage: zh_klei", b"InstalledLanguage: [zh_klei]")
    expect_code(lambda: core.validate_consumed_prefs(flow_value, expected), "prefs_yaml_ambiguous")
    alias_value = base.replace(b"InstalledLanguage: zh_klei", b"InstalledLanguage: *language")
    expect_code(lambda: core.validate_consumed_prefs(alias_value, expected), "prefs_yaml_ambiguous")
    explicit_key = base.replace(
        b"  InstalledLanguage: zh_klei\r\n",
        b"  ? InstalledLanguage\r\n  : zh_klei\r\n",
    )
    expect_code(lambda: core.validate_consumed_prefs(explicit_key, expected), "prefs_yaml_ambiguous")
    expect_code(lambda: core.path_id_upper(r"relative\save.sav"), "loader_path_not_absolute")
    expect_code(lambda: core.path_id_upper(r"~\save.sav"), "loader_path_not_absolute")
    return {"prefs_refusals": "passed"}


def test_player_refusals(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root, os.getpid())
    payload = material["captures"]["player_log_prefix"].payload
    wrong_loading = payload.replace(
        f"[INFO] Loading save file: {material['loader']}".encode("utf-8"),
        f"[INFO] Loading save file: {root / 'wrong.sav'}".encode("utf-8"),
    )
    expect_code(
        lambda: validate(material, with_capture(material, "player_log_prefix", wrong_loading)),
        "player_loading_path_mismatch",
    )
    wrong_pid = payload.replace(
        f"validation_process_id={os.getpid()}".encode("ascii"),
        b"validation_process_id=999999",
    )
    expect_code(
        lambda: validate(material, with_capture(material, "player_log_prefix", wrong_pid)),
        "player_verified_marker_count_invalid",
    )
    duplicate = payload + payload[payload.index(b"[ONI AI Probe]") :]
    expect_code(
        lambda: validate(material, with_capture(material, "player_log_prefix", duplicate)),
        "player_verified_marker_count_invalid",
    )
    failed = payload + (
        "[ONI AI Probe] STARTUP_BINDING_FAILED startup_binding_status=invalid "
        f"validation_run_id={material['binding']['validation_run_id']} "
        f"validation_process_id={os.getpid()}\n"
    ).encode("utf-8")
    expect_code(
        lambda: validate(material, with_capture(material, "player_log_prefix", failed)),
        "player_startup_failure_marker_present",
    )
    return {"player_refusals": "passed"}


def archive_payload(records: list[dict[str, Any]]) -> bytes:
    return b"".join(json_payload(record) + b"\n" for record in records)


def test_archive_refusals(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root, os.getpid())
    cases: list[tuple[str, Callable[[list[dict[str, Any]]], None], str]] = [
        ("sequence", lambda rows: rows[1].__setitem__("seq", 4), "archive_sequence_invalid"),
        ("reason", lambda rows: rows[0].__setitem__("reason", "game_lifecycle_patch"), "archive_session_start_invalid"),
        ("initial_hash", lambda rows: rows[0].__setitem__("save_id_hash", "sha256:" + "99" * 32), "archive_startup_save_identity_mismatch"),
        ("binding", lambda rows: rows[2].__setitem__("startup_binding_status", "invalid"), "archive_startup_field_mismatch"),
        ("pid", lambda rows: rows[1].__setitem__("validation_process_id", os.getpid() + 1), "archive_startup_field_mismatch"),
    ]
    for label, mutate, code in cases:
        rows = copy.deepcopy(material["prefix_records"])
        mutate(rows)
        expect_code(
            lambda rows=rows: validate(
                material,
                with_capture(material, "session_archive_prefix", archive_payload(rows)),
            ),
            code,
        )
        check(bool(label), "case label disappeared")
    no_snapshot = material["prefix_records"][:2]
    expect_code(
        lambda: validate(
            material,
            with_capture(material, "session_archive_prefix", archive_payload(no_snapshot)),
        ),
        "archive_minimal_prefix_invalid",
    )
    return {"archive_refusals": "passed"}


def validate_full_archive(material: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, int, int]:
    return core.validate_source_archive_records(
        rows,
        material["binding"],
        material["binding_hash"],
        os.getpid(),
    )


def test_source_archive_suffix_refusals(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root, os.getpid())
    session_id, prefix_count, snapshots = validate_full_archive(material, material["records"])
    check(
        session_id == material["session_id"] and prefix_count == 3 and snapshots == 1,
        "complete origin archive did not retain the minimal prefix boundary",
    )
    cases: list[tuple[str, Callable[[list[dict[str, Any]]], None], str]] = [
        (
            "result_path",
            lambda rows: rows[4].__setitem__("result_save_path_id", "sha256:" + "91" * 32),
            "archive_save_transition_invalid",
        ),
        (
            "before_hash",
            lambda rows: rows[4].__setitem__("before_save_id_hash", "sha256:" + "92" * 32),
            "archive_save_transition_invalid",
        ),
        (
            "written_hash",
            lambda rows: rows[4].__setitem__("written_save_id_hash", "sha256:" + "93" * 32),
            "archive_save_transition_invalid",
        ),
        (
            "suffix_active_path",
            lambda rows: rows[6].__setitem__("active_save_path_id", "sha256:" + "94" * 32),
            "archive_active_save_chain_mismatch",
        ),
        (
            "suffix_save_hash",
            lambda rows: rows[6].__setitem__("save_id_hash", "sha256:" + "95" * 32),
            "archive_active_save_chain_mismatch",
        ),
        (
            "suffix_run",
            lambda rows: rows[6].__setitem__("validation_run_id", "wrong-run"),
            "archive_startup_field_mismatch",
        ),
    ]
    for label, mutate, code in cases:
        rows = copy.deepcopy(material["records"])
        mutate(rows)
        expect_code(lambda rows=rows: validate_full_archive(material, rows), code)
        check(bool(label), "source archive suffix case label disappeared")

    missing_save = [*copy.deepcopy(material["prefix_records"]), copy.deepcopy(material["records"][-1])]
    missing_save[-1].update(
        {
            "seq": 4,
            "active_save_path_id": material["binding"]["requested_loader_path_id_upper"],
            "save_id_hash": material["binding"]["requested_loader_save_sha256"],
        }
    )
    expect_code(
        lambda: validate_full_archive(material, missing_save),
        "archive_save_transition_count_invalid",
    )

    duplicate = copy.deepcopy(material["records"])
    second_start = copy.deepcopy(duplicate[3])
    second_save = copy.deepcopy(duplicate[4])
    next_hash = "sha256:" + "88" * 32
    second_start.update(
        {
            "seq": 6,
            "ts": "2026-08-31T04:00:03.250000Z",
            "before_save_id_hash": material["derivative_save_hash"],
            "active_save_path_id": material["derivative_path_id"],
            "save_id_hash": material["derivative_save_hash"],
        }
    )
    second_save.update(
        {
            "seq": 7,
            "ts": "2026-08-31T04:00:03.400000Z",
            "before_save_id_hash": material["derivative_save_hash"],
            "written_save_id_hash": next_hash,
            "active_save_id_hash": next_hash,
            "save_id_hash": next_hash,
        }
    )
    duplicate[5:5] = [second_start, second_save]
    duplicate[-2].update({"seq": 8, "save_id_hash": next_hash})
    duplicate[-1].update({"seq": 9, "save_id_hash": next_hash})
    expect_code(
        lambda: validate_full_archive(material, duplicate),
        "archive_save_transition_count_invalid",
    )

    before_snapshot = copy.deepcopy(material["records"])
    before_snapshot[2], before_snapshot[3] = before_snapshot[3], before_snapshot[2]
    before_snapshot[2]["ts"] = "2026-08-31T04:00:01.500000Z"
    for seq, row in enumerate(before_snapshot, start=1):
        row["seq"] = seq
        if row.get("event") == "state_snapshot":
            row["snapshot_id"] = material["session_id"] + f":{seq}"
    expect_code(
        lambda: validate_full_archive(material, before_snapshot),
        "archive_save_transition_before_origin_snapshot",
    )
    return {"complete_origin_suffix": "passed", "suffix_refusals": "passed"}


def write_live_fixture(root: pathlib.Path, material: dict[str, Any]) -> dict[str, pathlib.Path]:
    config = root / "runtime.json"
    binding = root / "binding.json"
    prefs = root / "kplayerprefs.yaml"
    player = root / "Player.log"
    bridge = root / "ai-bridge"
    archive = bridge / "sessions" / core.safe_session_name(material["session_id"]) / "state.jsonl"
    archive.parent.mkdir(parents=True)
    process_session_id = "process-20260831-115959-cafebabe"
    process_archive = bridge / "sessions" / process_session_id / "state.jsonl"
    process_archive.parent.mkdir(parents=True)
    config.write_bytes(material["captures"]["runtime_config_evidence"].payload)
    binding.write_bytes(material["captures"]["startup_binding"].payload)
    prefs.write_bytes(material["captures"]["consumed_prefs"].payload)
    player.write_bytes(material["captures"]["player_log_prefix"].payload)
    archive.write_bytes(material["full_archive_payload"])
    process_record = common_fields(material["binding"], os.getpid(), process_session_id)
    process_record.update(
        {
            "schema": "oni.event.v2",
            "event": "probe_loaded",
            "seq": 1,
            "ts": "2026-08-31T03:59:59.000000Z",
            "startup_binding_hash": material["binding_hash"],
            "startup_binding_status": "pending_request",
            "requested_save_path_id": None,
            "active_save_path_id": None,
            "save_id_hash": None,
        }
    )
    process_archive.write_bytes(json_payload(process_record) + b"\n")
    return {
        "config": config,
        "binding": binding,
        "prefs": prefs,
        "player": player,
        "bridge": bridge,
        "process_archive": process_archive,
    }


def test_archive_discovery_boundaries(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root / "material", os.getpid())
    paths = write_live_fixture(root / "live", material)
    discovered = core.discover_session_archive(
        paths["bridge"],
        material["binding"],
        material["binding_hash"],
        os.getpid(),
    )
    check(discovered.session_id == material["session_id"], "process archive displaced target archive")
    check(
        discovered.prefix_capture.payload == material["captures"]["session_archive_prefix"].payload,
        "discovery did not preserve the exact minimal startup prefix",
    )
    check(
        discovered.source_archive["file_sha256"] == core.sha256_bytes(material["full_archive_payload"])
        and discovered.source_archive["record_count"] == len(material["records"]),
        "discovery did not bind the complete origin archive",
    )

    # A later reload can retain the run/binding/PID anchors while its first
    # record names the derivative save.  It is not an ambiguous origin.
    later_id = "session-20260831-120010-feedface"
    later = common_fields(material["binding"], os.getpid(), later_id)
    later.update(
        {
            "schema": "oni.event.v2",
            "event": "session_started",
            "reason": "save_loader_load_complete",
            "seq": 1,
            "ts": "2026-08-31T04:00:10.000000Z",
            "startup_binding_hash": material["binding_hash"],
            "requested_save_path_id": material["binding"]["requested_loader_path_id_upper"],
            "active_save_path_id": material["derivative_path_id"],
            "save_id_hash": material["derivative_save_hash"],
        }
    )
    later_path = paths["bridge"] / "sessions" / later_id / "state.jsonl"
    later_path.parent.mkdir(parents=True)
    later_path.write_bytes(json_payload(later) + b"\n")
    discovered_with_later = core.discover_session_archive(
        paths["bridge"], material["binding"], material["binding_hash"], os.getpid()
    )
    check(
        discovered_with_later.session_id == material["session_id"],
        "later derivative archive made origin discovery ambiguous",
    )

    decoy_id = "session-decoy-same-anchors"
    decoy_rows = copy.deepcopy(material["records"])
    for row in decoy_rows:
        row["session_id"] = decoy_id
        if row.get("snapshot_id") is not None:
            row["snapshot_id"] = decoy_id + f":{row['seq']}"
    decoy_rows[0]["reason"] = "game_lifecycle_patch"
    decoy_path = paths["bridge"] / "sessions" / decoy_id / "state.jsonl"
    decoy_path.parent.mkdir(parents=True)
    decoy_path.write_bytes(archive_payload(decoy_rows))
    expect_code(
        lambda: core.discover_session_archive(
            paths["bridge"],
            material["binding"],
            material["binding_hash"],
            os.getpid(),
        ),
        "archive_session_start_invalid",
    )
    return {
        "process_archive_skipped": "passed",
        "later_derivative_archive_skipped": "passed",
        "same_anchor_decoy_failed_closed": "passed",
    }


def test_create_replay_and_collision(root: pathlib.Path) -> dict[str, str]:
    material = make_material(root / "material", os.getpid())
    paths = write_live_fixture(root / "live", material)
    output = root / "bundle"
    process = (
        core._windows_process_evidence(os.getpid())
        if os.name == "nt"
        else core._posix_process_evidence(os.getpid())
    )
    namespace = argparse.Namespace(
        runtime_config_evidence=str(paths["config"]),
        expected_runtime_config_evidence_sha256=material["runtime_hash"],
        startup_binding=str(paths["binding"]),
        expected_startup_binding_sha256=material["binding_hash"],
        consumed_prefs=str(paths["prefs"]),
        player_log=str(paths["player"]),
        bridge_dir=str(paths["bridge"]),
        expected_process_id=os.getpid(),
        expected_process_image_name=pathlib.Path(sys.executable).name,
        expected_process_image_path_id_upper=process.image_path_id_upper,
        expected_process_image_sha256=process.image_file_sha256,
        output_dir=str(output),
    )
    parser = core.build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    create_actions = {action.dest: action for action in subparsers.choices["create"]._actions}
    check(
        create_actions["expected_process_image_path_id_upper"].required
        and create_actions["expected_process_image_sha256"].required,
        "process-image external anchors are not required create CLI inputs",
    )
    report = core.create_command(namespace)
    check(output.is_dir(), "bundle was not published")
    check(report["status"] == "passed", "create did not pass")
    report_text = (output / core.REPORT_FILE).read_text(encoding="utf-8")
    report_document = json.loads(report_text)
    reported_strings = string_values(report_document)
    check(
        all(material["loader"].casefold() not in value.casefold() for value in reported_strings),
        "report leaked the raw loader path",
    )
    check(
        all(process.image_path.casefold() not in value.casefold() for value in reported_strings),
        "report leaked the raw executable path",
    )
    check(
        report_document["schema"] == "oni.startup-consumption-evidence.v3",
        "process anchors did not produce the explicit v3 evidence schema",
    )
    check(
        set(report_document["process"])
        == {
            "process_id",
            "image_name",
            "creation_token",
            "image_path_id_upper",
            "image_file_sha256",
        },
        "process evidence field set is incomplete or drifting",
    )
    check(
        report_document["process"]["image_path_id_upper"] == process.image_path_id_upper
        and report_document["process"]["image_file_sha256"] == process.image_file_sha256,
        "externally supplied process-image anchors were not frozen into the report",
    )
    check(
        set(report_document) == core.EVIDENCE_KEYS
        and set(report_document["source_archive"]) == core.SOURCE_ARCHIVE_KEYS,
        "v3 evidence/source archive field sets are incomplete or drifting",
    )
    check(
        report_document["source_archive"]["file_sha256"]
        == core.sha256_bytes(material["full_archive_payload"])
        and report_document["source_archive"]["record_count"] == len(material["records"])
        and report_document["source_archive"]["prefix_file_sha256"]
        == material["captures"]["session_archive_prefix"].sha256,
        "v3 report did not distinguish full origin custody from the frozen prefix",
    )
    check(
        (output / core.ARTIFACT_FILES["session_archive_prefix"]).read_bytes()
        == material["captures"]["session_archive_prefix"].payload,
        "bundle did not contain the exact minimal origin prefix bytes",
    )
    check(
        process != replace(process, creation_token="sha256:" + "01" * 32)
        and process != replace(process, image_path_id_upper="sha256:" + "02" * 32)
        and process != replace(process, image_file_sha256="sha256:" + "03" * 32),
        "process before/after equality omits creation, path, or image hash",
    )
    expect_code(
        lambda: core.query_process(
            os.getpid(),
            pathlib.Path(sys.executable).name,
            "sha256:" + "04" * 32,
            process.image_file_sha256,
        ),
        "validation_process_image_path_mismatch",
    )
    expect_code(
        lambda: core.query_process(
            os.getpid(),
            pathlib.Path(sys.executable).name,
            process.image_path_id_upper,
            "sha256:" + "05" * 32,
        ),
        "validation_process_image_hash_mismatch",
    )
    replay = core.verify_command(argparse.Namespace(bundle_dir=str(output)))
    check(replay["verification_mode"] == "offline_replay", "offline replay did not pass")
    check(
        replay["schema"] == "oni.startup-consumption-evidence-replay-verification.v3",
        "replay verification schema was not bumped with evidence v3",
    )
    expect_code(lambda: core.create_command(namespace), "bundle_already_exists")

    report_path = output / core.REPORT_FILE
    report_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    original_report = report_path.read_bytes()
    extra_process_field = json.loads(original_report)
    extra_process_field["process"]["raw_image_path"] = process.image_path
    unhashed = dict(extra_process_field)
    del unhashed["evidence_hash"]
    extra_process_field["evidence_hash"] = core.canonical_hash(unhashed)
    report_path.write_bytes(json_payload(extra_process_field))
    expect_code(
        lambda: core.verify_command(argparse.Namespace(bundle_dir=str(output))),
        "evidence_process_invalid",
    )
    report_path.write_bytes(original_report)

    real_query_process = core.query_process
    query_count = 0

    def query_with_creation_drift(*args: Any, **kwargs: Any) -> core.ProcessEvidence:
        nonlocal query_count
        query_count += 1
        observed = real_query_process(*args, **kwargs)
        if query_count == 2:
            return replace(observed, creation_token="sha256:" + "06" * 32)
        return observed

    core.query_process = query_with_creation_drift
    try:
        expect_code(lambda: core.capture_live_sources(namespace), "validation_process_changed")
    finally:
        core.query_process = real_query_process

    target = output / core.ARTIFACT_FILES["session_archive_prefix"]
    target.chmod(stat.S_IWRITE | stat.S_IREAD)
    original = target.read_bytes()
    target.write_bytes(original.replace(b'"seq":3', b'"seq":9', 1))
    expect_code(
        lambda: core.verify_command(argparse.Namespace(bundle_dir=str(output))),
        "evidence_artifact_hash_mismatch",
    )
    return {"create_replay_collision": "passed"}


def test_publish_rename_attacks(root: pathlib.Path) -> dict[str, str]:
    if os.name != "nt":
        material = make_material(root / "material", os.getpid())
        expect_code(
            lambda: core.publish_bundle(root / "unsupported", material["captures"], {"schema": "test"}),
            "bundle_atomic_noreplace_unsupported",
        )
        return {"non_windows_fail_closed": "passed"}

    material = make_material(root / "material", os.getpid())
    captures = material["captures"]
    real_rename = core.os.rename

    write_failure_output = root / "write-failure"
    real_write = core.os.write
    def fail_write(_descriptor: int, _payload: Any) -> int:
        raise OSError("injected write failure")

    core.os.write = fail_write
    try:
        try:
            core.publish_bundle(write_failure_output, captures, {"schema": "test"})
        except OSError:
            pass
        else:
            raise AssertionError("injected create-only write failure was accepted")
    finally:
        core.os.write = real_write
    check(not write_failure_output.exists(), "write-failure rollback published a partial bundle")
    check(not list(root.glob(".write-failure.tmp-*")), "write-failure rollback left an owned temp directory")

    collision_output = root / "race-collision"
    def collide(source: Any, destination: Any) -> None:
        destination_path = pathlib.Path(destination)
        destination_path.mkdir()
        (destination_path / "attacker.txt").write_text("attacker", encoding="utf-8")
        raise FileExistsError(17, "collision")

    core.os.rename = collide
    try:
        expect_code(
            lambda: core.publish_bundle(collision_output, captures, {"schema": "test"}),
            "bundle_already_exists",
        )
    finally:
        core.os.rename = real_rename
    check((collision_output / "attacker.txt").is_file(), "collision cleanup deleted attacker directory")
    check(not list(root.glob(".race-collision.tmp-*")), "collision rollback left an owned temp directory")

    source_output = root / "source-reappeared"
    source_paths: list[pathlib.Path] = []
    def reappear_source(source: Any, destination: Any) -> None:
        source_path = pathlib.Path(source)
        source_paths.append(source_path)
        real_rename(source, destination)
        source_path.mkdir()
        (source_path / "attacker.txt").write_text("attacker", encoding="utf-8")

    core.os.rename = reappear_source
    try:
        expect_code(
            lambda: core.publish_bundle(source_output, captures, {"schema": "test"}),
            "bundle_source_path_reappeared",
        )
    finally:
        core.os.rename = real_rename
    check(source_paths and (source_paths[0] / "attacker.txt").is_file(), "source-swap cleanup deleted attacker directory")
    check(not source_output.exists(), "source-swap rollback retained the owned published directory")

    destination_output = root / "destination-swapped"
    holding = root / "owned-quarantine"
    def swap_destination(source: Any, destination: Any) -> None:
        destination_path = pathlib.Path(destination)
        real_rename(source, destination)
        real_rename(destination, holding)
        destination_path.mkdir()
        (destination_path / "attacker.txt").write_text("attacker", encoding="utf-8")

    core.os.rename = swap_destination
    try:
        expect_code(
            lambda: core.publish_bundle(destination_output, captures, {"schema": "test"}),
            "bundle_destination_identity_changed",
        )
    finally:
        core.os.rename = real_rename
    check((destination_output / "attacker.txt").is_file(), "destination-swap cleanup deleted attacker directory")
    check(holding.is_dir(), "destination-swap test lost the original owned directory")
    return {
        "write_failure_rolled_back": "passed",
        "rename_collision_preserved_attacker": "passed",
        "source_swap_preserved_attacker": "passed",
        "destination_swap_preserved_attacker": "passed",
    }


def make_writable_tree(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for item in path.rglob("*"):
        try:
            item.chmod(stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if item.is_dir() else 0))
        except OSError:
            pass
    try:
        path.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def main() -> int:
    scenarios: list[tuple[str, Callable[[pathlib.Path], dict[str, str]]]] = [
        ("positive", test_positive),
        ("binding_and_intent_tamper", test_binding_and_intent_tamper),
        ("v2_v5_bootstrap_contract_refusals", test_v2_v5_bootstrap_contract_refusals),
        ("consumed_prefs_refusals", test_prefs_refusals),
        ("player_log_refusals", test_player_refusals),
        ("session_archive_refusals", test_archive_refusals),
        ("source_archive_suffix_refusals", test_source_archive_suffix_refusals),
        ("archive_discovery_boundaries", test_archive_discovery_boundaries),
        ("create_replay_collision", test_create_replay_and_collision),
        ("publish_rename_attacks", test_publish_rename_attacks),
    ]
    results: dict[str, Any] = {}
    temporary = pathlib.Path(tempfile.mkdtemp(prefix="oni-startup-evidence-"))
    try:
        for name, scenario in scenarios:
            case_root = temporary / name
            case_root.mkdir(parents=True)
            results[name] = scenario(case_root)
    finally:
        make_writable_tree(temporary)
        shutil.rmtree(temporary, ignore_errors=False)
    print(
        json.dumps(
            {
                "schema": "oni.startup-consumption-evidence-verification.v3",
                "status": "passed",
                "scenario_count": len(scenarios),
                "assertion_count": ASSERTIONS,
                "results": results,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
