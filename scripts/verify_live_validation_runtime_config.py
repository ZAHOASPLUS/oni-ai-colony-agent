#!/usr/bin/env python3
"""Verify a configured G0 runtime or run the isolated TestRoot regression suite."""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Optional


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import configure_live_validation_runtime as core


REPORT = PROJECT_ROOT / "reports" / "live-validation-runtime-config-verification.json"
SELF_TEST_SCHEMA = "oni.live-validation-runtime-config-self-test.v5"


class VerificationFailure(RuntimeError):
    """An offline fixture did not prove the expected behavior."""


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def write_bytes(path: pathlib.Path, payload: bytes, *, stamp: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.utime(path, ns=(stamp, stamp))


def write_json(path: pathlib.Path, value: Mapping[str, Any], *, stamp: int) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    write_bytes(path, payload, stamp=stamp)


def disabled_policy() -> dict[str, Any]:
    return {
        "schema": core.POLICY_SCHEMA,
        "actions_enabled": False,
        "suggestions_enabled": False,
        "allowed_save_hashes": [],
        "allowed_actions": ["focus_note", "noop"],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
        "generated_at": "2026-08-31T00:00:00+00:00",
    }


def mods_document() -> dict[str, Any]:
    mods: list[dict[str, Any]] = []
    for index in range(21):
        mods.append(
            {
                "label": {
                    "distribution_platform": 1,
                    "id": str(1_000_000_000 + index),
                    "title": f"Production-shaped fixture mod {index:02d}",
                    "version": 1_700_000_000 + index,
                },
                "status": 1,
                "enabled": index % 2 == 0,
                "enabledForDlc": ["EXPANSION1_ID"],
                "crash_count": 0,
                "reinstall_path": None,
                "staticID": f"fixture.production.{index:02d}",
            }
        )
    mods.append(
        {
            "label": {
                "distribution_platform": 0,
                "id": "oni-ai-colony-agent",
                "title": "ONI AI Probe",
                "version": 7,
            },
            "status": 1,
            "enabled": False,
            "enabledForDlc": ["EXPANSION1_ID"],
            "crash_count": 0,
            "reinstall_path": None,
            "staticID": core.PROBE_STATIC_ID,
        }
    )
    return {
        "version": 1,
        "mods": mods,
        "mod_load_in_progress": False,
    }


def mods_bytes() -> bytes:
    # Match the live file's UTF-8/no-BOM, CRLF, and no-final-newline shape.
    return json.dumps(mods_document(), ensure_ascii=False, indent=2).replace("\n", "\r\n").encode("utf-8")


def state(path: pathlib.Path) -> dict[str, Any]:
    return core.file_state(path)


def config_record(oni: pathlib.Path, relative: str, environment: pathlib.Path) -> dict[str, Any]:
    path = oni / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
    if not path.exists():
        return {
            "relative": relative,
            "path_id_lower": core.path_id(path, contract="lower"),
            "path_id_upper": core.path_id(path, contract="upper"),
            "exists": False,
            "backup_relative": None,
        }
    backup_relative = f"backup/config/{relative}"
    backup = environment / pathlib.Path(*pathlib.PurePosixPath(backup_relative).parts)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    return {
        "relative": relative,
        "path_id_lower": core.path_id(path, contract="lower"),
        "path_id_upper": core.path_id(path, contract="upper"),
        "exists": True,
        "state": state(path),
        "backup_relative": backup_relative,
    }


def fixture_node_identity(path: pathlib.Path) -> list[int]:
    return core.node_identity_list(os.lstat(path))


def open_parent_finalize_lock(
    fixture: Mapping[str, Any],
    *,
    owner_token: str = "fedcba9876543210fedcba9876543210",
) -> tuple[pathlib.Path, int, core.ActiveParentFinalizeLock]:
    path = pathlib.Path(fixture["environment"]) / core.LOCK_NAME
    descriptor = core.open_exclusive_shared_delete(path)
    payload = core.canonical_bytes(
        {
            "schema": "oni.live-validation-finalize-lock.v1",
            "run_id": fixture["run_id"],
            "owner_token": owner_token,
        }
    ) + b"\n"
    core.write_descriptor_all(descriptor, payload)
    os.fsync(descriptor)
    metadata = os.fstat(descriptor)
    proof = core.ActiveParentFinalizeLock(
        fd=descriptor,
        identity=core.full_file_identity(metadata),
        payload=payload,
        payload_sha256=core.sha256_bytes(payload),
        owner_token=owner_token,
    )
    return path, descriptor, proof


def append_fixture_restore_journal(
    fixture: Mapping[str, Any],
    event: str = "finalize_preflight_passed",
    data: Optional[Mapping[str, Any]] = None,
    *,
    canonical: bool = True,
) -> dict[str, Any]:
    journal_path = pathlib.Path(fixture["environment"]) / "restore-journal.jsonl"
    entries = core.verify_journal(journal_path)
    unsigned: dict[str, Any] = {
        "schema": core.JOURNAL_SCHEMA,
        "index": len(entries) + 1,
        "recorded_at": "2026-08-31T00:01:00+00:00",
        "event": event,
        "data": dict(data or {"fixture_preflight": True}),
        "previous_entry_hash": entries[-1]["entry_hash"],
    }
    entry = dict(unsigned)
    entry["entry_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned))
    if canonical:
        payload = core.canonical_bytes(entry) + b"\n"
    else:
        payload = json.dumps(
            entry,
            ensure_ascii=False,
            sort_keys=False,
            separators=(", ", ": "),
        ).encode("utf-8") + b"\n"
    with journal_path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def rewrite_fixture_restore_journal_in_place(
    fixture: Mapping[str, Any],
    entries: Iterable[Mapping[str, Any]],
) -> None:
    journal_path = pathlib.Path(fixture["environment"]) / "restore-journal.jsonl"
    payload = b"".join(core.canonical_bytes(dict(entry)) + b"\n" for entry in entries)
    with journal_path.open("r+b") as handle:
        handle.seek(0)
        handle.truncate(0)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def fixture_tree_snapshot(
    root: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = os.lstat(root)
    entries: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        relative = path.relative_to(root).as_posix()
        item_metadata = os.lstat(path)
        if path.is_dir():
            entries.append(
                {
                    "kind": "directory",
                    "relative": relative,
                    "last_write_ns": int(item_metadata.st_mtime_ns),
                }
            )
            kind = "directory"
        else:
            item_state = state(path)
            entries.append(
                {
                    "kind": "file",
                    "relative": relative,
                    "length": item_state["length"],
                    "last_write_ns": item_state["last_write_ns"],
                    "sha256": item_state["sha256"],
                }
            )
            kind = "file"
        identities.append(
            {
                "relative": relative,
                "kind": kind,
                "identity": fixture_node_identity(path),
            }
        )
    snapshot = {
        "exists": True,
        "root_last_write_ns": int(metadata.st_mtime_ns),
        "entries": entries,
        "file_count": sum(item["kind"] == "file" for item in entries),
        "total_bytes": sum(
            int(item.get("length", 0)) for item in entries if item["kind"] == "file"
        ),
    }
    snapshot["tree_hash"] = core.sha256_bytes(core.canonical_bytes(snapshot))
    identity = {
        "exists": True,
        "root_identity": fixture_node_identity(root),
        "entries": identities,
    }
    return snapshot, identity


def fixture_lock_binding(
    transaction_root: pathlib.Path,
    *,
    epoch: int,
    purpose: str,
    transaction_id: str,
    plan_hash: str,
    intent_hash: str,
    stamp: int,
) -> dict[str, Any]:
    attempt = transaction_root / "environment-lock-attempts" / f"{epoch:04d}-{purpose}"
    attempt.mkdir(parents=True, exist_ok=False)
    released_lock = attempt / "released.lock"
    payload = core.json_document_bytes(
        {
            "schema": "oni.live-runtime-bootstrap-environment-lock.v1",
            "transaction_id": transaction_id,
            "plan_hash": plan_hash,
            "epoch": epoch,
            "purpose": purpose,
            "intent_hash": intent_hash,
        }
    )
    write_bytes(released_lock, payload, stamp=stamp)
    claim: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_LOCK_CLAIM_SCHEMA,
        "transaction_id": transaction_id,
        "plan_hash": plan_hash,
        "epoch": epoch,
        "purpose": purpose,
        "intent_hash": intent_hash,
        "payload_sha256": core.sha256_bytes(payload),
        "lock_identity": fixture_node_identity(released_lock),
    }
    claim["claim_hash"] = core.sha256_bytes(core.canonical_bytes(claim))
    release: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_LOCK_RELEASE_SCHEMA,
        "transaction_id": transaction_id,
        "plan_hash": plan_hash,
        "epoch": epoch,
        "purpose": purpose,
        "intent_hash": intent_hash,
        "claim_hash": claim["claim_hash"],
        "released_identity": list(claim["lock_identity"]),
        "payload_sha256": claim["payload_sha256"],
    }
    release["release_hash"] = core.sha256_bytes(core.canonical_bytes(release))
    write_json(attempt / "claim.json", claim, stamp=stamp + 1)
    write_json(attempt / "released.json", release, stamp=stamp + 2)
    return {
        f"{purpose if purpose == 'install' else 'ready'}_lock_epoch": epoch,
        f"{purpose if purpose == 'install' else 'ready'}_lock_claim": claim,
        f"{purpose if purpose == 'install' else 'ready'}_lock_claim_hash": claim[
            "claim_hash"
        ],
        f"{purpose if purpose == 'install' else 'ready'}_lock_release": release,
        f"{purpose if purpose == 'install' else 'ready'}_lock_release_hash": release[
            "release_hash"
        ],
    }


def publish_runtime_bootstrap_fixture(
    *,
    case: pathlib.Path,
    oni: pathlib.Path,
    evidence: pathlib.Path,
    environment: pathlib.Path,
    run_id: str,
    manifest_path: pathlib.Path,
    probe: pathlib.Path,
    bridge: pathlib.Path,
    release_dll: pathlib.Path,
    bootstrap: Mapping[str, Any],
    stamp: int,
) -> tuple[pathlib.Path, str]:
    transaction_root = evidence / run_id / "runtime-bootstrap"
    transaction_root.mkdir(parents=True, exist_ok=False)
    manifest = core.load_json(manifest_path, "fixture environment manifest")
    manifest_payload, manifest_state = core.read_snapshot(manifest_path)
    journal_path = environment / "restore-journal.jsonl"
    journal_payload, journal_state = core.read_snapshot(journal_path)
    journal = core.verify_journal(journal_path)
    environment_binding = {
        "path_id_lower": core.path_id(manifest_path, contract="lower"),
        "sha256": core.sha256_bytes(manifest_payload),
        "length": len(manifest_payload),
        "last_write_ns": manifest_state["last_write_ns"],
        "identity": fixture_node_identity(manifest_path),
        "initial_restore_journal_entry_hash": journal[0]["entry_hash"],
        "restore_journal_identity": fixture_node_identity(journal_path),
        "restore_journal_sha256": core.sha256_bytes(journal_payload),
        "restore_journal_length": len(journal_payload),
        "restore_journal_tip_hash": journal[-1]["entry_hash"],
        "restore_journal_entry_count": 1,
        "environment_directory_identity": fixture_node_identity(environment),
    }
    release_parent = release_dll.parent
    release_parent_metadata = os.lstat(release_parent)
    release_artifacts: dict[str, Any] = {}
    for artifact_name in core.RUNTIME_BOOTSTRAP_ARTIFACT_NAMES:
        path = release_parent / artifact_name
        item_state = state(path)
        release_artifacts[artifact_name] = {
            "path_id_lower": core.path_id(path, contract="lower"),
            "path_id_upper": core.path_id(path, contract="upper"),
            "parent_identity": fixture_node_identity(release_parent),
            "parent_last_write_ns": int(release_parent_metadata.st_mtime_ns),
            "identity": fixture_node_identity(path),
            "length": item_state["length"],
            "last_write_ns": item_state["last_write_ns"],
            "sha256": item_state["sha256"],
        }
    probe_snapshot = manifest["backup"]["probe"]["snapshot"]
    _current_probe_snapshot, probe_prestate_identity = fixture_tree_snapshot(probe)
    absent_identity = {"exists": False, "entries": []}
    prestate = {
        "probe": {
            "snapshot": copy.deepcopy(probe_snapshot),
            "identity": probe_prestate_identity,
        },
        "old_bridge": {
            "snapshot": copy.deepcopy(manifest["backup"]["old_bridge"]["snapshot"]),
            "identity": copy.deepcopy(absent_identity),
        },
        "archive_root": {
            "snapshot": copy.deepcopy(
                manifest["backup"]["bridge_archive_root"]["snapshot"]
            ),
            "identity": copy.deepcopy(absent_identity),
        },
        "parent_directories": {
            key: {
                "identity": fixture_node_identity(path),
                "last_write_ns": int(os.lstat(path).st_mtime_ns),
            }
            for key, path in {
                "oni_root": oni,
                "probe_container": oni / "mods",
                "probe_parent": oni / "mods" / "Dev",
            }.items()
        },
    }
    targets = {
        "probe_relative": core.PROBE_RELATIVE.as_posix(),
        "bridge_relative": core.BRIDGE_RELATIVE.as_posix(),
        "archive_root_relative": "ai-bridge-archives",
        "evidence_relative": f"{run_id}/runtime-bootstrap",
    }
    inputs = {
        "environment_manifest": environment_binding,
        "release_artifacts": release_artifacts,
    }
    seed = {
        "schema": "oni.live-runtime-bootstrap-seed.v1",
        "run_id": run_id,
        "expected_mod_version": core.EXPECTED_MOD_VERSION,
        "inputs": inputs,
        "prestate": prestate,
        "targets": targets,
    }
    seed_hash = core.sha256_bytes(core.canonical_bytes(seed))
    transaction_id = "rtb-" + seed_hash.removeprefix("sha256:")[:24]
    action_policy = disabled_policy()
    validation_policy = {
        "schema": "oni.validation-control-policy.v1",
        "controls_enabled": False,
        "test_run_id": None,
        "allowed_operations": [],
        "allowed_save_hashes": [],
        "allowed_save_path_ids": [],
        "allowed_target_save_path_ids": [],
        "generated_at": manifest["created_at"],
        "expires_at": None,
    }
    plan: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_PLAN_SCHEMA,
        "transaction_id": transaction_id,
        "run_id": run_id,
        "expected_mod_version": core.EXPECTED_MOD_VERSION,
        "seed_hash": seed_hash,
        "inputs": inputs,
        "prestate": prestate,
        "targets": targets,
        "desired": {
            "bootstrap": dict(bootstrap),
            "action_policy": action_policy,
            "validation_control_policy": validation_policy,
            "archive_relative": None,
        },
        "physical_steps": [
            "stage_probe",
            "stage_bridge",
            "ensure_archive_root_if_needed",
            "archive_old_bridge_if_present",
            "recover_old_probe_if_present",
            "install_probe",
            "install_bridge",
        ],
    }
    plan["plan_hash"] = core.sha256_bytes(core.canonical_bytes(plan))
    intent: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_INTENT_SCHEMA,
        "transaction_id": transaction_id,
        "run_id": run_id,
        "plan_hash": plan["plan_hash"],
        "environment_manifest_sha256": environment_binding["sha256"],
        "release_dll_sha256": release_artifacts["OniAiProbeMod.dll"]["sha256"],
        "expected_mod_version": core.EXPECTED_MOD_VERSION,
        "confirm_live_run_id": run_id,
        "operation": "install_prebuilt_release",
    }
    intent["intent_hash"] = core.sha256_bytes(core.canonical_bytes(intent))
    producer_path = PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py"
    producer_state = state(producer_path)
    producer = {
        "length": producer_state["length"],
        "sha256": producer_state["sha256"],
    }
    stage: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_INSTALL_STAGE_SCHEMA,
        "run_id": run_id,
        "transaction_id": transaction_id,
        "plan_hash": plan["plan_hash"],
        "install_intent_hash": intent["intent_hash"],
        "transaction_path_id_lower": core.path_id(transaction_root, contract="lower"),
        "stage_nonce": "0123456789abcdef0123456789abcdef",
        "install_intent": intent,
        "install_plan": plan,
        "producer": producer,
    }
    stage["stage_hash"] = core.sha256_bytes(core.canonical_bytes(stage))
    stage_path = environment / "runtime-bootstrap-install-stage.json"
    write_json(stage_path, stage, stamp=stamp)

    probe_tree = core.capture_exact_probe_tree(probe)
    bridge_tree = core.capture_runtime_bridge_content(bridge)
    commit_entry: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_JOURNAL_SCHEMA,
        "index": 8,
        "transaction_id": transaction_id,
        "plan_hash": plan["plan_hash"],
        "event": "commit_verified",
        "data": {
            "probe_tree_content_hash": probe_tree["content_hash"],
            "bridge_tree_content_hash": bridge_tree["content_hash"],
            "archive_relative": None,
            "old_probe_recovery": "recovery/old-probe",
        },
        "previous_entry_hash": core.sha256_bytes(b"fixture-previous-entry"),
    }
    commit_hash = core.sha256_bytes(core.canonical_bytes(commit_entry))
    install_binding = fixture_lock_binding(
        transaction_root,
        epoch=0,
        purpose="install",
        transaction_id=transaction_id,
        plan_hash=plan["plan_hash"],
        intent_hash=intent["intent_hash"],
        stamp=stamp + 10,
    )
    installed_artifacts = copy.deepcopy(probe_tree["artifacts"])
    bootstrap_path = bridge / "v27-runtime-bootstrap.json"
    candidate: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_RESULT_CANDIDATE_SCHEMA,
        "status": "candidate_not_ready",
        "classification": "static_probe",
        "run_id": run_id,
        "transaction_id": transaction_id,
        "plan_hash": plan["plan_hash"],
        "environment_manifest_sha256": environment_binding["sha256"],
        "release_dll_sha256": installed_artifacts["OniAiProbeMod.dll"]["sha256"],
        "expected_mod_version": core.EXPECTED_MOD_VERSION,
        "probe_tree_content_hash": probe_tree["content_hash"],
        "installed_probe_artifacts": installed_artifacts,
        "installed_probe_tree_identity": probe_tree["identity"],
        "bridge_tree_content_hash": bridge_tree["content_hash"],
        "bootstrap_document_hash": core.sha256_bytes(core.canonical_bytes(bootstrap)),
        "bootstrap_file_sha256": core.sha256_file(bootstrap_path),
        "archive_relative": None,
        "old_probe_recovery_relative": "recovery/old-probe",
        "install_commit_entry": commit_entry,
        "install_commit_entry_hash": commit_hash,
        "install_stage_sha256": core.sha256_file(stage_path),
        "install_stage_hash": stage["stage_hash"],
        "producer": producer,
        **install_binding,
        "physical_commit_complete": True,
        "game_launched": False,
    }
    candidate["candidate_hash"] = core.sha256_bytes(core.canonical_bytes(candidate))
    candidate_path = transaction_root / "result-candidate.json"
    write_json(candidate_path, candidate, stamp=stamp + 20)
    ready_binding = fixture_lock_binding(
        transaction_root,
        epoch=1,
        purpose="replay",
        transaction_id=transaction_id,
        plan_hash=plan["plan_hash"],
        intent_hash=candidate["candidate_hash"],
        stamp=stamp + 30,
    )
    result: dict[str, Any] = {
        key: copy.deepcopy(value)
        for key, value in candidate.items()
        if key
        not in {
            "schema",
            "status",
            "candidate_hash",
        }
    }
    result.update(
        {
            "schema": core.RUNTIME_BOOTSTRAP_RESULT_SCHEMA,
            "status": "installed_ready",
            **ready_binding,
            "candidate_sha256": core.sha256_file(candidate_path),
            "candidate_hash": candidate["candidate_hash"],
            "shared_lock_released": True,
        }
    )
    result["result_hash"] = core.sha256_bytes(core.canonical_bytes(result))
    result_path = transaction_root / "result.json"
    write_json(result_path, result, stamp=stamp + 40)
    return result_path, core.sha256_file(result_path)


def make_fixture(
    base: pathlib.Path,
    name: str,
    *,
    run_id: str = "g0-config-fixture",
    prefs_builder: Optional[Callable[[pathlib.Path, pathlib.Path], bytes]] = None,
    save_payload: bytes = b"fixture-save-v1\x00",
) -> dict[str, Any]:
    case = base / name
    oni = case / "oni-user"
    evidence = case / "evidence"
    boundary = evidence / "live-boundaries"
    environment = evidence / run_id / "environment"
    managed = case / "game" / "OxygenNotIncluded_Data" / "Managed"
    save_dir = oni / "save_files" / "FixtureColony"
    source = save_dir / "Original.sav"
    loader_stem = f"{run_id}-loader"
    loader = save_dir / f"{loader_stem}.sav"
    stamp = 1_725_000_000_000_000_000

    for directory in (oni, evidence, boundary, environment, managed, save_dir):
        directory.mkdir(parents=True, exist_ok=True)
    main_assembly = managed / "Assembly-CSharp.dll"
    firstpass_assembly = managed / "Assembly-CSharp-firstpass.dll"
    write_bytes(
        main_assembly,
        b"MZ fixture Assembly-CSharp KleiVersion.ChangeList UInt32 744825\x00",
        stamp=stamp + 1,
    )
    write_bytes(
        firstpass_assembly,
        b"MZ fixture Assembly-CSharp-firstpass KPlayerPrefs AutoResumeSaveFile\x00",
        stamp=stamp + 2,
    )
    write_bytes(source, save_payload, stamp=stamp + 10)
    write_bytes(source.with_suffix(".png"), b"fixture-preview-v1\x89PNG", stamp=stamp + 20)
    write_bytes(loader, source.read_bytes(), stamp=stamp + 30)
    write_bytes(loader.with_suffix(".png"), source.with_suffix(".png").read_bytes(), stamp=stamp + 40)
    prefs = (
        prefs_builder(source, loader)
        if prefs_builder is not None
        else f"strings:\r\n  InstalledLanguage: zh_klei\r\n  SaveFilenameKey/: {source}\r\nints:\r\n  SESSION_ID: 1\r\nfloats:\r\n  Volume_Master: 1\r\n".encode("utf-8")
    )
    write_bytes(oni / "kplayerprefs.yaml", prefs, stamp=stamp + 50)
    write_bytes(oni / "mods" / "mods.json", mods_bytes(), stamp=stamp + 60)
    write_bytes(oni / "mods" / "mods.json.bak", mods_bytes(), stamp=stamp + 70)
    write_bytes(oni / "mods" / "config.json", b'{"fixture":true}\r\n', stamp=stamp + 80)

    probe = oni / "mods" / "Dev" / "oni-ai-colony-agent"
    release = case / "workspace" / "mod" / "OniAiProbeMod.dll"
    probe_dll = b"fixture-release-probe-0.7.0\x00"
    probe_pdb = b"fixture-release-probe-symbols-0.7.0\x00"
    mod_yaml_bytes = b'title: "ONI AI Probe"\r\nstaticID: "ass.OniAiProbe"\r\n'
    mod_info_bytes = b"APIVersion: 2\r\n"
    write_bytes(release, probe_dll, stamp=stamp + 85)
    write_bytes(release.with_name("OniAiProbeMod.pdb"), probe_pdb, stamp=stamp + 86)
    write_bytes(release.with_name("mod.yaml"), mod_yaml_bytes, stamp=stamp + 87)
    write_bytes(release.with_name("mod_info.yaml"), mod_info_bytes, stamp=stamp + 88)
    write_bytes(probe / "OniAiProbeMod.dll", probe_dll, stamp=stamp + 90)
    write_bytes(probe / "OniAiProbeMod.pdb", probe_pdb, stamp=stamp + 95)
    write_bytes(probe / "mod.yaml", mod_yaml_bytes, stamp=stamp + 100)
    write_bytes(probe / "mod_info.yaml", mod_info_bytes, stamp=stamp + 110)

    bridge = oni / "ai-bridge"
    for queue in (
        "pending",
        "approving",
        "approved",
        "executing",
        "confirming",
        "done",
        "failed",
        "rejected",
        "quarantine",
    ):
        (bridge / "commands" / queue).mkdir(parents=True, exist_ok=True)
    for queue in ("approved", "inflight", "done", "failed", "quarantine"):
        (bridge / "validation-controls" / queue).mkdir(parents=True, exist_ok=True)
    (bridge / "evidence").mkdir(parents=True, exist_ok=True)
    write_json(bridge / "action-policy.json", disabled_policy(), stamp=stamp + 120)
    write_json(
        bridge / "validation-control-policy.json",
        {
            "schema": "oni.validation-control-policy.v1",
            "controls_enabled": False,
            "test_run_id": None,
            "allowed_operations": [],
            "allowed_save_hashes": [],
            "allowed_save_path_ids": [],
            "allowed_target_save_path_ids": [],
            "generated_at": "2026-08-31T00:00:00+00:00",
            "expires_at": None,
        },
        stamp=stamp + 125,
    )
    dll_hash = core.sha256_file(probe / "OniAiProbeMod.dll")
    bootstrap = {
        "schema": core.BOOTSTRAP_SCHEMA,
        "run_id": run_id,
        "prepared_at": "2026-08-31T00:00:00+00:00",
        "expected_mod_version": core.EXPECTED_MOD_VERSION,
        "bridge_dir": str(bridge),
        "archived_previous_bridge": None,
        "built_dll_sha256": dll_hash,
        "installed_dll_sha256": dll_hash,
        "installed_release": True,
        "action_policy_disabled": True,
    }
    write_json(bridge / "v27-runtime-bootstrap.json", bootstrap, stamp=stamp + 130)

    pre = {
        "schema": core.BOUNDARY_SCHEMA,
        "phase": "pre",
        "run_id": run_id,
        "oni_root_id": core.path_id(oni, contract="lower"),
        "path_disclosure": "identifiers_only",
    }
    pre_path = boundary / f"{run_id}-pre.json"
    write_json(pre_path, pre, stamp=stamp + 140)
    clone = {
        "schema": core.CLONE_SCHEMA,
        "run_id": run_id,
        "created_at": "2026-08-31T00:00:00+00:00",
        "created": True,
        "oni_root_id": core.path_id(oni, contract="lower"),
        "report_root_id": core.path_id(evidence, contract="lower"),
        "path_disclosure": "identifiers_only",
        "target_stem_id": core.text_id(loader_stem),
        "source": {
            "save": {"sha256": core.sha256_file(source)},
            "preview": {"sha256": core.sha256_file(source.with_suffix(".png"))},
        },
        "target": {
            "directory_path_id": core.path_id(save_dir, contract="lower"),
            "save_path_id": core.path_id(loader, contract="lower"),
            "preview_path_id": core.path_id(loader.with_suffix(".png"), contract="lower"),
            "save_sha256": core.sha256_file(loader),
            "preview_sha256": core.sha256_file(loader.with_suffix(".png")),
        },
    }
    clone_path = evidence / run_id / "test-clone-manifest.json"
    write_json(clone_path, clone, stamp=stamp + 150)

    configs = [config_record(oni, relative, environment) for relative in core.CONFIG_RELATIVES]
    probe_backup_snapshot, _probe_backup_identity = fixture_tree_snapshot(probe)
    absent_snapshot = {
        "exists": False,
        "entries": [],
        "tree_hash": core.sha256_bytes(b"[]"),
    }
    manifest = {
        "schema": core.ENVIRONMENT_SCHEMA,
        "run_id": run_id,
        "created_at": "2026-08-31T00:00:00+00:00",
        "mode": "test_fixture",
        "path_disclosure": "root_ids_and_relative_paths_only",
        "roots": {
            "oni_root_id_lower": core.path_id(oni, contract="lower"),
            "oni_root_id_upper": core.path_id(oni, contract="upper"),
            "evidence_root_id_lower": core.path_id(evidence, contract="lower"),
            "evidence_root_id_upper": core.path_id(evidence, contract="upper"),
            "same_local_volume": True,
        },
        "bindings": {
            "pre_boundary": {
                "path_id": core.path_id(pre_path, contract="lower"),
                "sha256": core.sha256_file(pre_path),
                "oni_root_id": pre["oni_root_id"],
            },
            "clone_manifest": {
                "path_id": core.path_id(clone_path, contract="lower"),
                "sha256": core.sha256_file(clone_path),
                "target_stem_id": clone["target_stem_id"],
            },
            "source_save": {
                "path_id_lower": core.path_id(source, contract="lower"),
                "path_id_upper": core.path_id(source, contract="upper"),
                "state": state(source),
            },
            "source_preview": {
                "path_id_lower": core.path_id(source.with_suffix(".png"), contract="lower"),
                "path_id_upper": core.path_id(source.with_suffix(".png"), contract="upper"),
                "state": state(source.with_suffix(".png")),
            },
            "loader_save": {
                "path_id_lower": core.path_id(loader, contract="lower"),
                "path_id_upper": core.path_id(loader, contract="upper"),
                "stem_id": core.text_id(loader_stem),
                "state": state(loader),
            },
            "loader_preview": {
                "path_id_lower": core.path_id(loader.with_suffix(".png"), contract="lower"),
                "path_id_upper": core.path_id(loader.with_suffix(".png"), contract="upper"),
                "state": state(loader.with_suffix(".png")),
            },
        },
        "backup": {
            "configs": configs,
            "probe": {
                "relative": core.PROBE_RELATIVE.as_posix(),
                "path_id_lower": core.path_id(probe, contract="lower"),
                "path_id_upper": core.path_id(probe, contract="upper"),
                "backup_relative": "backup/probe",
                "snapshot": probe_backup_snapshot,
            },
            "old_bridge": {
                "relative": "ai-bridge",
                "snapshot": copy.deepcopy(absent_snapshot),
            },
            "bridge_archive_root": {
                "relative": "ai-bridge-archives",
                "snapshot": copy.deepcopy(absent_snapshot),
            },
        },
        "preconditions": {
            "processes": {"oxygen_not_included_count": 0, "bridge_count": 0},
            "bridge_gate": {"policy_disabled": True},
        },
    }
    manifest_path = environment / "environment-manifest.json"
    write_json(manifest_path, manifest, stamp=stamp + 160)
    journal_unsigned = {
        "schema": core.JOURNAL_SCHEMA,
        "index": 1,
        "recorded_at": "2026-08-31T00:00:00+00:00",
        "event": "environment_prepared",
        "data": {
            "environment_manifest_sha256": core.sha256_file(manifest_path),
            "pre_boundary_sha256": core.sha256_file(pre_path),
            "clone_manifest_sha256": core.sha256_file(clone_path),
        },
        "previous_entry_hash": None,
    }
    journal = dict(journal_unsigned)
    journal["entry_hash"] = core.sha256_bytes(core.canonical_bytes(journal_unsigned))
    write_bytes(
        environment / "restore-journal.jsonl",
        core.canonical_bytes(journal) + b"\n",
        stamp=stamp + 170,
    )
    runtime_bootstrap_result, expected_runtime_bootstrap_result_sha256 = (
        publish_runtime_bootstrap_fixture(
            case=case,
            oni=oni,
            evidence=evidence,
            environment=environment,
            run_id=run_id,
            manifest_path=manifest_path,
            probe=probe,
            bridge=bridge,
            release_dll=release,
            bootstrap=bootstrap,
            stamp=stamp + 200,
        )
    )
    process_fixture = case / "process-idle.json"
    write_json(
        process_fixture,
        {
            "schema": "oni.process-state-fixture.v1",
            "oxygen_not_included_count": 0,
            "bridge_count": 0,
        },
        stamp=stamp + 180,
    )
    return {
        "case": case,
        "oni": oni,
        "evidence": evidence,
        "boundary": boundary,
        "environment": environment,
        "manifest": manifest_path,
        "expected_environment_manifest_sha256": core.sha256_file(manifest_path),
        "runtime_bootstrap_result": runtime_bootstrap_result,
        "expected_runtime_bootstrap_result_sha256": (
            expected_runtime_bootstrap_result_sha256
        ),
        "pre": pre_path,
        "clone": clone_path,
        "source": source,
        "loader": loader,
        "loader_stem": loader_stem,
        "process": process_fixture,
        "run_id": run_id,
        "release_dll": release,
        "expected_release_dll_sha256": core.sha256_file(release),
        "managed": managed,
        "main_assembly": main_assembly,
        "firstpass_assembly": firstpass_assembly,
        "main_assembly_sha256": core.sha256_file(main_assembly),
        "firstpass_assembly_sha256": core.sha256_file(firstpass_assembly),
    }


def coherently_forge_environment_manifest_and_journal(
    fixture: Mapping[str, Any],
    marker: str,
) -> str:
    """Change both local claims while deliberately retaining the old external anchor."""

    manifest_path = pathlib.Path(fixture["manifest"])
    manifest = core.load_json(manifest_path, "fixture environment manifest")
    manifest["coherent_external_anchor_attack"] = marker
    manifest_stamp = os.lstat(manifest_path).st_mtime_ns + 10_000
    write_json(manifest_path, manifest, stamp=manifest_stamp)
    forged_manifest_hash = core.sha256_file(manifest_path)
    assert_true(
        forged_manifest_hash != fixture["expected_environment_manifest_sha256"],
        "coherent manifest forgery accidentally retained the external anchor",
    )

    journal_path = pathlib.Path(fixture["environment"]) / "restore-journal.jsonl"
    raw_lines = journal_path.read_bytes().splitlines()
    assert_true(len(raw_lines) == 1, "fixture restore journal shape changed")
    journal = core.parse_json_payload(raw_lines[0], "fixture restore journal")
    data = dict(journal.get("data") or {})
    data["environment_manifest_sha256"] = forged_manifest_hash
    unsigned = dict(journal)
    unsigned.pop("entry_hash", None)
    unsigned["data"] = data
    forged_journal = dict(unsigned)
    forged_journal["entry_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned))
    write_bytes(
        journal_path,
        core.canonical_bytes(forged_journal) + b"\n",
        stamp=os.lstat(journal_path).st_mtime_ns + 10_000,
    )
    assert_true(
        core.verify_journal(journal_path)[0]["data"][
            "environment_manifest_sha256"
        ]
        == forged_manifest_hash,
        "coherent journal forgery fixture was not internally valid",
    )
    return forged_manifest_hash


def rebuild_candidate_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "schema": core.RUNTIME_BOOTSTRAP_RESULT_CANDIDATE_SCHEMA,
        "status": "candidate_not_ready",
        "classification": result["classification"],
        "run_id": result["run_id"],
        "transaction_id": result["transaction_id"],
        "plan_hash": result["plan_hash"],
        "environment_manifest_sha256": result["environment_manifest_sha256"],
        "release_dll_sha256": result["release_dll_sha256"],
        "expected_mod_version": result["expected_mod_version"],
        "probe_tree_content_hash": result["probe_tree_content_hash"],
        "installed_probe_artifacts": copy.deepcopy(result["installed_probe_artifacts"]),
        "installed_probe_tree_identity": copy.deepcopy(
            result["installed_probe_tree_identity"]
        ),
        "bridge_tree_content_hash": result["bridge_tree_content_hash"],
        "bootstrap_document_hash": result["bootstrap_document_hash"],
        "bootstrap_file_sha256": result["bootstrap_file_sha256"],
        "archive_relative": result["archive_relative"],
        "old_probe_recovery_relative": result["old_probe_recovery_relative"],
        "install_commit_entry": copy.deepcopy(result["install_commit_entry"]),
        "install_commit_entry_hash": result["install_commit_entry_hash"],
        "install_stage_sha256": result["install_stage_sha256"],
        "install_stage_hash": result["install_stage_hash"],
        "producer": copy.deepcopy(result["producer"]),
        "install_lock_epoch": result["install_lock_epoch"],
        "install_lock_claim": copy.deepcopy(result["install_lock_claim"]),
        "install_lock_claim_hash": result["install_lock_claim_hash"],
        "install_lock_release": copy.deepcopy(result["install_lock_release"]),
        "install_lock_release_hash": result["install_lock_release_hash"],
        "physical_commit_complete": True,
        "game_launched": False,
    }
    candidate["candidate_hash"] = core.sha256_bytes(core.canonical_bytes(candidate))
    return candidate


def rewrite_runtime_bootstrap_result(
    fixture: Mapping[str, Any],
    result: dict[str, Any],
) -> str:
    unsigned = dict(result)
    unsigned.pop("result_hash", None)
    result["result_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned))
    path = pathlib.Path(fixture["runtime_bootstrap_result"])
    path.write_bytes(core.json_document_bytes(result))
    return core.sha256_file(path)


def frozen_runtime_bootstrap_records(
    fixture: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result_path = pathlib.Path(fixture["runtime_bootstrap_result"])
    result_payload = result_path.read_bytes()
    result = core.parse_json_payload(result_payload, "fixture runtime bootstrap result")
    result_record = {
        "path_id_lower": core.path_id(result_path, contract="lower"),
        "external_expected_sha256": fixture[
            "expected_runtime_bootstrap_result_sha256"
        ],
        "raw_sha256": core.sha256_bytes(result_payload),
        "raw_byte_length": len(result_payload),
        "raw_utf8_base64": base64.b64encode(result_payload).decode("ascii"),
        "result_hash": result["result_hash"],
        "transaction_id": result["transaction_id"],
        "probe_tree_content_hash": result["probe_tree_content_hash"],
        "document": result,
    }
    bootstrap_path = pathlib.Path(fixture["oni"]) / "ai-bridge" / "v27-runtime-bootstrap.json"
    bootstrap_payload = bootstrap_path.read_bytes()
    bootstrap = core.parse_json_payload(bootstrap_payload, "fixture runtime bootstrap")
    bootstrap_record = {
        "path_id_lower": core.path_id(bootstrap_path, contract="lower"),
        "raw_sha256": core.sha256_bytes(bootstrap_payload),
        "raw_byte_length": len(bootstrap_payload),
        "raw_utf8_base64": base64.b64encode(bootstrap_payload).decode("ascii"),
        "document_hash": core.sha256_bytes(core.canonical_bytes(bootstrap)),
        "document": bootstrap,
    }
    return result_record, bootstrap_record


def reseal_fixture_runtime_bootstrap_result(
    fixture: dict[str, Any],
    result: dict[str, Any],
    *,
    rebuild_candidate: bool = True,
) -> None:
    if rebuild_candidate:
        candidate = rebuild_candidate_projection(result)
        result["candidate_hash"] = candidate["candidate_hash"]
        result["candidate_sha256"] = core.sha256_bytes(
            core.json_document_bytes(candidate)
        )
    fixture["expected_runtime_bootstrap_result_sha256"] = (
        rewrite_runtime_bootstrap_result(fixture, result)
    )


def without_cli_option(argv: Iterable[str], option: str) -> list[str]:
    values = list(argv)
    index = values.index(option)
    del values[index : index + 2]
    return values


def assert_parser_requires(parser: argparse.ArgumentParser, argv: Iterable[str]) -> None:
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(list(argv))
        except SystemExit as exc:
            assert_true(exc.code == 2, "missing required CLI anchor did not exit 2")
            return
    raise VerificationFailure("missing required CLI anchor was accepted")


def arguments(fixture: Mapping[str, Any], *, apply: bool = False, confirm: bool = True) -> argparse.Namespace:
    argv = [
        "--run-id",
        str(fixture["run_id"]),
        "--oni-root",
        str(fixture["oni"]),
        "--evidence-root",
        str(fixture["evidence"]),
        "--boundary-report-root",
        str(fixture["boundary"]),
        "--environment-manifest",
        str(fixture["manifest"]),
        "--expected-environment-manifest-sha256",
        str(fixture["expected_environment_manifest_sha256"]),
        "--runtime-bootstrap-result",
        str(fixture["runtime_bootstrap_result"]),
        "--expected-runtime-bootstrap-result-sha256",
        str(fixture["expected_runtime_bootstrap_result_sha256"]),
        "--pre-boundary",
        str(fixture["pre"]),
        "--clone-manifest",
        str(fixture["clone"]),
        "--source-save",
        str(fixture["source"]),
        "--loader-save",
        str(fixture["loader"]),
        "--loader-stem",
        str(fixture["loader_stem"]),
        "--release-dll",
        str(fixture["release_dll"]),
        "--expected-release-dll-sha256",
        str(fixture["expected_release_dll_sha256"]),
        "--game-managed-dir",
        str(fixture["managed"]),
        "--test-managed-assembly-csharp-sha256",
        str(fixture["main_assembly_sha256"]),
        "--test-managed-assembly-csharp-firstpass-sha256",
        str(fixture["firstpass_assembly_sha256"]),
        "--test-root",
        str(fixture["case"]),
        "--process-state-fixture",
        str(fixture["process"]),
    ]
    if apply:
        argv.append("--apply")
        if confirm:
            argv.extend(("--confirm-live-run-id", str(fixture["run_id"])))
    return core.build_parser().parse_args(argv)


def verification_arguments(
    fixture: Mapping[str, Any],
    evidence_sha256: str,
    binding_sha256: Optional[str] = None,
) -> argparse.Namespace:
    args = arguments(fixture)
    args.expected_runtime_config_evidence_sha256 = evidence_sha256
    args.expected_startup_binding_sha256 = binding_sha256 or core.file_state(
        startup_binding_path(fixture)
    )["sha256"]
    return args


def common_cli_arguments(fixture: Mapping[str, Any]) -> list[str]:
    return [
        "--run-id",
        str(fixture["run_id"]),
        "--oni-root",
        str(fixture["oni"]),
        "--evidence-root",
        str(fixture["evidence"]),
        "--boundary-report-root",
        str(fixture["boundary"]),
        "--environment-manifest",
        str(fixture["manifest"]),
        "--expected-environment-manifest-sha256",
        str(fixture["expected_environment_manifest_sha256"]),
        "--runtime-bootstrap-result",
        str(fixture["runtime_bootstrap_result"]),
        "--expected-runtime-bootstrap-result-sha256",
        str(fixture["expected_runtime_bootstrap_result_sha256"]),
        "--pre-boundary",
        str(fixture["pre"]),
        "--clone-manifest",
        str(fixture["clone"]),
        "--source-save",
        str(fixture["source"]),
        "--loader-save",
        str(fixture["loader"]),
        "--loader-stem",
        str(fixture["loader_stem"]),
        "--expected-mod-version",
        core.EXPECTED_MOD_VERSION,
        "--release-dll",
        str(fixture["release_dll"]),
        "--expected-release-dll-sha256",
        str(fixture["expected_release_dll_sha256"]),
        "--game-managed-dir",
        str(fixture["managed"]),
        "--test-managed-assembly-csharp-sha256",
        str(fixture["main_assembly_sha256"]),
        "--test-managed-assembly-csharp-firstpass-sha256",
        str(fixture["firstpass_assembly_sha256"]),
        "--test-root",
        str(fixture["case"]),
        "--process-state-fixture",
        str(fixture["process"]),
    ]


def tree_snapshot(root: pathlib.Path) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        relative = path.relative_to(root).as_posix()
        metadata = os.lstat(path)
        if path.is_file():
            result[relative] = (core.sha256_file(path), metadata.st_size, metadata.st_mtime_ns)
        elif path.is_dir():
            result[relative] = ("directory", 0, 0)
    return result


def expect_blocked(callback: Any, contains: str) -> core.ConfigError:
    try:
        callback()
    except core.ConfigError as exc:
        assert_true(contains.casefold() in str(exc).casefold(), f"unexpected blocker: {exc}")
        return exc
    raise VerificationFailure(f"expected blocker containing {contains!r}")


def expect_blocked_chain(callback: Any, contains: str) -> core.ConfigError:
    """Require a fail-closed error while retaining nested publication cause evidence."""

    try:
        callback()
    except core.ConfigError as exc:
        messages: list[str] = []
        current: Optional[BaseException] = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            messages.append(str(current))
            current = current.__cause__ or current.__context__
        assert_true(
            any(contains.casefold() in message.casefold() for message in messages),
            f"unexpected blocker chain: {' <- '.join(messages)}",
        )
        return exc
    raise VerificationFailure(f"expected blocker chain containing {contains!r}")


def original_config_state(fixture: Mapping[str, Any]) -> dict[str, tuple[bytes, dict[str, Any]]]:
    return {
        relative: core.read_snapshot(
            pathlib.Path(fixture["oni"]) / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        )
        for relative in core.TARGET_CONFIG_RELATIVES
    }


def assert_original_restored(fixture: Mapping[str, Any], original: Mapping[str, tuple[bytes, dict[str, Any]]]) -> None:
    for relative, expected in original.items():
        current = core.read_snapshot(
            pathlib.Path(fixture["oni"]) / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        )
        assert_true(current == expected, f"{relative} bytes/mtime were not transactionally restored")
    assert_true(
        not (pathlib.Path(fixture["environment"]) / core.EVIDENCE_NAME).exists(),
        "failed apply left runtime config evidence",
    )
    assert_true(
        not startup_binding_path(fixture).exists(),
        "failed apply left runtime startup binding",
    )
    assert_true(not list(pathlib.Path(fixture["oni"]).rglob(".g0-*.tmp")), "failed apply left temp files")


def runtime_evidence_path(fixture: Mapping[str, Any]) -> pathlib.Path:
    return pathlib.Path(fixture["environment"]) / core.EVIDENCE_NAME


def startup_binding_path(fixture: Mapping[str, Any]) -> pathlib.Path:
    return pathlib.Path(fixture["oni"]) / "ai-bridge" / core.STARTUP_BINDING_NAME


def recovery_evidence_paths(fixture: Mapping[str, Any]) -> list[pathlib.Path]:
    return sorted(pathlib.Path(fixture["environment"]).glob("runtime-config-recovery-*.json"))


def assert_recovery_evidence(fixture: Mapping[str, Any]) -> dict[str, Any]:
    paths = recovery_evidence_paths(fixture)
    assert_true(len(paths) == 1, "outcome-unknown apply did not create exactly one recovery record")
    payload, _state = core.read_snapshot(paths[0])
    value = core.parse_json_payload(payload, "recovery evidence")
    claimed = value.pop("document_hash", None)
    assert_true(
        claimed == core.sha256_bytes(core.canonical_bytes(value)),
        "recovery evidence self-hash is invalid",
    )
    assert_true(
        value.get("status") == "outcome_unknown"
        and value.get("automatic_overwrite_performed") is False
        and value.get("requires_manual_recovery_review") is True,
        "recovery evidence did not preserve fail-closed semantics",
    )
    return value


def assert_runtime_configs_configured(fixture: Mapping[str, Any]) -> None:
    prefs = (pathlib.Path(fixture["oni"]) / "kplayerprefs.yaml").read_text(encoding="utf-8")
    assert_true(str(fixture["loader"]) in prefs, "outcome-unknown path lost configured loader bytes")
    mods = json.loads(
        (pathlib.Path(fixture["oni"]) / "mods" / "mods.json").read_text(encoding="utf-8")
    )
    assert_true(
        [item["staticID"] for item in mods["mods"] if item["enabled"]] == [core.PROBE_STATIC_ID],
        "outcome-unknown path lost the Probe-only mod configuration",
    )


def rewrite_evidence(path: pathlib.Path, value: dict[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("document_hash", None)
    value["document_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned))
    payload = core.json_document_bytes(value)
    path.write_bytes(payload)
    return core.sha256_bytes(payload)


def rewrite_binding(path: pathlib.Path, value: dict[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("document_hash", None)
    value["document_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned))
    payload = core.json_document_bytes(value)
    path.write_bytes(payload)
    return core.sha256_bytes(payload)


def scenario_positive(base: pathlib.Path) -> dict[str, str]:
    fixture = make_fixture(base, "positive")
    before_mods = json.loads((pathlib.Path(fixture["oni"]) / "mods" / "mods.json").read_text(encoding="utf-8"))
    result = core.configure(arguments(fixture, apply=True))
    assert_true(result["status"] == "configured", "positive apply did not configure")
    verified = core.verify_evidence(
        verification_arguments(fixture, result["runtime_config_evidence_sha256"])
    )
    assert_true(verified["status"] == "passed", "post-config evidence did not verify")
    prefs_path = pathlib.Path(fixture["oni"]) / "kplayerprefs.yaml"
    prefs_payload = prefs_path.read_bytes()
    expected_prefs = (
        f"strings:\r\n"
        f"  InstalledLanguage: zh_klei\r\n"
        f"  SaveFilenameKey/: {fixture['loader']}\r\n"
        f"  AutoResumeSaveFile: {fixture['loader']}\r\n"
        f"ints:\r\n"
        f"  SESSION_ID: 1\r\n"
        f"floats:\r\n"
        f"  Volume_Master: 1\r\n"
    ).encode("utf-8")
    assert_true(prefs_payload == expected_prefs, "prelaunch kplayerprefs desired bytes are not exact")
    expected_policy = core.loader_policy_from_kplayerprefs(
        expected_prefs,
        pathlib.Path(fixture["loader"]),
        core.capture_managed_assembly_binding(
            core.resolve_roots(arguments(fixture))
        ),
    )
    evidence = json.loads(runtime_evidence_path(fixture).read_text(encoding="utf-8"))
    binding_path = startup_binding_path(fixture)
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    intent = evidence.get("startup_intent")
    expected_manifest_anchor = fixture["expected_environment_manifest_sha256"]
    assert_true(
        result.get("environment_manifest_sha256")
        == verified.get("environment_manifest_sha256")
        == evidence.get("environment_manifest_sha256")
        == binding.get("environment_manifest_sha256")
        == intent.get("environment_manifest_sha256")
        == expected_manifest_anchor,
        "external environment manifest anchor was not projected end-to-end",
    )
    assert_true(
        isinstance(intent, dict)
        and set(intent) == core.STARTUP_INTENT_KEYS
        and intent.get("schema") == core.STARTUP_INTENT_SCHEMA
        and core.STARTUP_NONCE_RE.fullmatch(str(intent.get("startup_nonce") or ""))
        is not None,
        "startup intent shape or nonce is invalid",
    )
    intent_unsigned = dict(intent)
    intent_claimed = intent_unsigned.pop("document_hash")
    assert_true(
        intent_claimed == core.sha256_bytes(core.canonical_bytes(intent_unsigned))
        and result.get("startup_intent_hash") == intent_claimed,
        "startup intent self-hash/result anchor diverged",
    )
    binding_unsigned = dict(binding)
    binding_claimed = binding_unsigned.pop("document_hash")
    assert_true(
        set(binding) == core.STARTUP_BINDING_KEYS
        and binding.get("schema") == core.STARTUP_BINDING_SCHEMA
        and binding_claimed == core.sha256_bytes(core.canonical_bytes(binding_unsigned)),
        "startup binding exact shape/self-hash is invalid",
    )
    assert_true(
        result.get("runtime_config_evidence_sha256")
        == core.sha256_bytes(runtime_evidence_path(fixture).read_bytes())
        == binding.get("runtime_config_evidence_sha256")
        and result.get("startup_binding_sha256")
        == core.sha256_bytes(binding_path.read_bytes())
        and result.get("startup_nonce") == binding.get("startup_nonce")
        == intent.get("startup_nonce")
        and binding.get("startup_intent_hash") == intent_claimed,
        "runtime evidence/binding raw anchors or cross-links diverged",
    )
    loader_state = json.loads(
        pathlib.Path(fixture["manifest"]).read_text(encoding="utf-8")
    )["bindings"]["loader_save"]["state"]
    assert_true(
        intent.get("requested_loader_path_id_lower")
        == result.get("requested_loader_path_id_lower")
        and intent.get("requested_loader_path_id_upper")
        == binding.get("requested_loader_path_id_upper")
        == result.get("requested_loader_path_id_upper")
        and intent.get("requested_loader_save_sha256")
        == binding.get("requested_loader_save_sha256")
        == result.get("requested_loader_save_sha256")
        == loader_state["sha256"]
        and intent.get("requested_loader_save_length")
        == binding.get("requested_loader_save_length")
        == result.get("requested_loader_save_length")
        == loader_state["length"],
        "startup pair lost loader path-id/content binding",
    )
    assert_true(
        intent.get("expected_game_build") == core.EXPECTED_GAME_BUILD
        and intent.get("expected_mod_version") == core.EXPECTED_MOD_VERSION
        and intent.get("expected_probe_dll_sha256")
        == result.get("expected_probe_dll_sha256")
        == fixture["expected_release_dll_sha256"],
        "startup pair lost game/mod/Probe binding",
    )
    result_document = json.loads(
        pathlib.Path(fixture["runtime_bootstrap_result"]).read_text(
            encoding="utf-8"
        )
    )
    expected_runtime_projection = {
        "runtime_bootstrap_result_path_id_lower": core.path_id(
            pathlib.Path(fixture["runtime_bootstrap_result"]), contract="lower"
        ),
        "runtime_bootstrap_result_sha256": fixture[
            "expected_runtime_bootstrap_result_sha256"
        ],
        "runtime_bootstrap_result_hash": result_document["result_hash"],
        "runtime_bootstrap_transaction_id": result_document["transaction_id"],
        "installed_probe_tree_content_hash": result_document[
            "probe_tree_content_hash"
        ],
    }
    assert_true(
        all(
            intent.get(key)
            == binding.get(key)
            == result.get(key)
            == verified.get(key)
            == value
            for key, value in expected_runtime_projection.items()
        )
        and all(
            evidence["runtime"]["runtime_bootstrap_result"].get(
                {
                    "runtime_bootstrap_result_path_id_lower": "path_id_lower",
                    "runtime_bootstrap_result_sha256": "raw_sha256",
                    "runtime_bootstrap_result_hash": "result_hash",
                    "runtime_bootstrap_transaction_id": "transaction_id",
                    "installed_probe_tree_content_hash": "probe_tree_content_hash",
                }[key]
            )
            == value
            for key, value in expected_runtime_projection.items()
        ),
        "runtime bootstrap result projection did not cross all v5/v2 layers",
    )
    assert_true(
        str(fixture["loader"]) not in json.dumps(result, ensure_ascii=False)
        and str(fixture["loader"]) not in json.dumps(verified, ensure_ascii=False)
        and str(fixture["loader"]) not in json.dumps(intent, ensure_ascii=False)
        and str(fixture["loader"]) not in json.dumps(binding, ensure_ascii=False),
        "startup outputs disclosed the raw loader save path",
    )
    assert_true(
        result.get("loader_policy") == expected_policy
        and evidence.get("loader_policy") == expected_policy
        and verified.get("loader_policy") == expected_policy,
        "apply/evidence/independent verification loader policies diverged",
    )
    assert_true(
        expected_policy.get("save_filename_key_count") == 1
        and expected_policy.get("auto_resume_save_file_key_count") == 1
        and expected_policy.get("save_filename_key_exact_loader") is True
        and expected_policy.get("auto_resume_save_file_exact_loader") is True
        and expected_policy.get("expected_one_shot_consumption") is True
        and expected_policy.get("built_in_semantics")
        == core.bound_builtin_auto_resume_semantics(
            core.capture_managed_assembly_binding(
                core.resolve_roots(arguments(fixture))
            )
        ),
        "loader policy lost exact-path or built-in one-shot semantics",
    )
    after_mods = json.loads((pathlib.Path(fixture["oni"]) / "mods" / "mods.json").read_text(encoding="utf-8"))
    assert_true(
        [item["staticID"] for item in after_mods["mods"] if item["enabled"]] == [core.PROBE_STATIC_ID],
        "Probe was not the only enabled mod",
    )
    for before, after in zip(before_mods["mods"], after_mods["mods"]):
        projected = dict(before)
        projected["enabled"] = after["enabled"]
        assert_true(projected == after, "a non-enabled mod field changed")
    return {
        "apply": "passed",
        "post_verify": "passed",
        "startup_nonce_format": "passed",
        "startup_intent_self_hash": "passed",
        "startup_binding_self_hash": "passed",
        "startup_pair_raw_hash_crosslink": "passed",
        "startup_bridge_arguments": "passed",
        "external_environment_manifest_anchor_projection": "passed",
        "runtime_bootstrap_result_projection": "passed",
        "raw_loader_path_redaction": "passed",
        "exact_desired_bytes": "passed",
        "loader_policy_three_way_match": "passed",
        "loader_only": "passed",
        "other_mods_disabled": "passed",
    }


def scenario_cli_surface(base: pathlib.Path) -> dict[str, str]:
    fixture = make_fixture(base, "cli-surface")
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    configure_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "configure_live_validation_runtime.py"),
        *common_cli_arguments(fixture),
        "--apply",
        "--confirm-live-run-id",
        str(fixture["run_id"]),
    ]
    configured = subprocess.run(
        configure_command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert_true(configured.returncode == 0, f"configure CLI failed: {configured.stdout}")
    configure_result = json.loads(configured.stdout)
    assert_true(configure_result.get("status") == "configured", "configure CLI result was not configured")

    verify_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "verify_live_validation_runtime_config.py"),
        "verify",
        *common_cli_arguments(fixture),
        "--expected-runtime-config-evidence-sha256",
        configure_result["runtime_config_evidence_sha256"],
        "--expected-startup-binding-sha256",
        configure_result["startup_binding_sha256"],
    ]
    verified = subprocess.run(
        verify_command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert_true(verified.returncode == 0, f"verify CLI failed: {verified.stdout}")
    verify_result = json.loads(verified.stdout)
    assert_true(
        verify_result.get("status") == "passed"
        and verify_result.get("runtime_config_evidence_sha256")
        == configure_result["runtime_config_evidence_sha256"],
        "verify CLI did not consume the exact configure evidence anchor",
    )
    assert_true(
        verify_result.get("startup_binding_sha256")
        == configure_result["startup_binding_sha256"],
        "verify CLI did not consume the exact startup binding anchor",
    )
    return {"configure_apply_cli": "passed", "verify_historical_cli": "passed"}


def scenario_environment_manifest_external_anchor(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    parser_case = make_fixture(base, "manifest-anchor-cli-required")
    common_without_anchor = without_cli_option(
        common_cli_arguments(parser_case),
        "--expected-environment-manifest-sha256",
    )
    assert_parser_requires(core.build_parser(), common_without_anchor)
    results["configure_cli_anchor_required"] = "blocked_by_argparse"
    assert_parser_requires(
        build_parser(),
        [
            "verify",
            *common_without_anchor,
            "--expected-runtime-config-evidence-sha256",
            "sha256:" + "1" * 64,
            "--expected-startup-binding-sha256",
            "sha256:" + "2" * 64,
        ],
    )
    results["independent_verify_cli_anchor_required"] = "blocked_by_argparse"

    invalid_values: tuple[tuple[str, Any], ...] = (
        ("missing_prefix", "0" * 64),
        ("uppercase_hex", "sha256:" + "A" * 64),
        ("short_hash", "sha256:" + "0" * 63),
        ("bool_value", True),
        ("bool_string", "true"),
    )
    for label, invalid in invalid_values:
        fixture = make_fixture(base, f"manifest-anchor-format-{label}")
        original = original_config_state(fixture)
        args = arguments(fixture, apply=True)
        args.expected_environment_manifest_sha256 = invalid
        expect_blocked(lambda args=args: core.configure(args), "external environment manifest anchor")
        assert_original_restored(fixture, original)
        results[f"configure_{label}"] = "blocked_zero_write"

    wrong_case = make_fixture(base, "manifest-anchor-wrong")
    wrong_original = original_config_state(wrong_case)
    wrong_args = arguments(wrong_case, apply=True)
    wrong_anchor = "sha256:" + "0" * 64
    assert_true(
        wrong_anchor != wrong_case["expected_environment_manifest_sha256"],
        "wrong manifest anchor fixture accidentally matched",
    )
    wrong_args.expected_environment_manifest_sha256 = wrong_anchor
    expect_blocked(lambda: core.configure(wrong_args), "external SHA-256 anchor")
    assert_original_restored(wrong_case, wrong_original)
    results["configure_wrong_anchor"] = "blocked_zero_write"

    coherent_case = make_fixture(base, "manifest-anchor-coherent-forgery")
    coherent_original = original_config_state(coherent_case)
    coherently_forge_environment_manifest_and_journal(coherent_case, "initial")
    expect_blocked(
        lambda: core.configure(arguments(coherent_case, apply=True)),
        "external SHA-256 anchor",
    )
    assert_original_restored(coherent_case, coherent_original)
    results["coherent_manifest_and_journal_forgery"] = "blocked_zero_write"

    plan_case = make_fixture(base, "manifest-anchor-plan-apply")
    plan_original = original_config_state(plan_case)
    plan = core.configure(arguments(plan_case))
    assert_true(
        plan.get("environment_manifest_sha256")
        == plan_case["expected_environment_manifest_sha256"],
        "WhatIf did not project the external manifest anchor",
    )
    coherently_forge_environment_manifest_and_journal(plan_case, "plan-to-apply")
    expect_blocked(
        lambda: core.configure(arguments(plan_case, apply=True)),
        "external SHA-256 anchor",
    )
    assert_original_restored(plan_case, plan_original)
    results["plan_to_apply_toctou"] = "blocked_zero_write"

    apply_stages: tuple[tuple[str, str, str], ...] = (
        ("lock_revalidation", "after_lock_acquire", "blocked_zero_write"),
        ("first_write", "before_commit", "blocked_zero_write"),
        ("between_replaces", "after_replace_1", "owned_rollback"),
        (
            "evidence_prepublication",
            "before_managed_prepublication_check",
            "owned_rollback",
        ),
        (
            "startup_binding_prepublication",
            "before_binding_managed_prepublication_check",
            "evidence_revoked_owned_rollback",
        ),
        ("final_pair_readback", "before_pair_postcheck", "pair_revoked_owned_rollback"),
        (
            "before_lock_release",
            "before_lock_release",
            "pair_revoked_owned_rollback",
        ),
        (
            "lock_release_callback",
            "after_lock_release_check",
            "pair_revoked_owned_rollback",
        ),
        (
            "after_lock_name_release",
            "after_lock_name_release",
            "pair_revoked_owned_rollback",
        ),
        (
            "before_lock_descriptor_release",
            "before_lock_descriptor_release",
            "pair_revoked_owned_rollback",
        ),
        (
            "after_lock_descriptor_release",
            "after_lock_descriptor_release",
            "pair_revoked_owned_rollback",
        ),
        (
            "configured_result_return_gap",
            "before_configured_result",
            "pair_revoked_owned_rollback",
        ),
    )
    for label, event_name, classification in apply_stages:
        fixture = make_fixture(base, f"manifest-anchor-toctou-{label}")
        original = original_config_state(fixture)
        fired = False

        def forge_at_stage(
            event: str,
            _ctx: core.RuntimeContext,
            *,
            expected_event: str = event_name,
            target_fixture: Mapping[str, Any] = fixture,
            marker: str = label,
        ) -> None:
            nonlocal fired
            if event == expected_event and not fired:
                fired = True
                coherently_forge_environment_manifest_and_journal(
                    target_fixture,
                    marker,
                )

        core._TEST_HOOK = forge_at_stage
        try:
            expect_blocked_chain(
                lambda fixture=fixture: core.configure(arguments(fixture, apply=True)),
                "external SHA-256 anchor",
            )
        finally:
            core._TEST_HOOK = None
        assert_true(fired, f"manifest anchor TOCTOU hook {event_name} did not fire")
        assert_original_restored(fixture, original)
        results[f"toctou_{label}"] = classification

    verify_case = make_fixture(base, "manifest-anchor-independent-verify")
    configured = core.configure(arguments(verify_case, apply=True))
    pair_before = {
        "evidence": runtime_evidence_path(verify_case).read_bytes(),
        "binding": startup_binding_path(verify_case).read_bytes(),
    }
    configs_before = {
        relative: core.read_snapshot(
            pathlib.Path(verify_case["oni"])
            / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        )
        for relative in core.TARGET_CONFIG_RELATIVES
    }
    verify_wrong = verification_arguments(
        verify_case,
        configured["runtime_config_evidence_sha256"],
        configured["startup_binding_sha256"],
    )
    verify_wrong.expected_environment_manifest_sha256 = wrong_anchor
    expect_blocked(
        lambda: core.verify_evidence(verify_wrong),
        "external SHA-256 anchor",
    )
    results["independent_verify_wrong_anchor"] = "blocked_read_only"

    for label, invalid in (
        ("bool_confusion", False),
        ("bool_string_confusion", "false"),
        ("uppercase_hash", "sha256:" + "A" * 64),
    ):
        verify_format = verification_arguments(
            verify_case,
            configured["runtime_config_evidence_sha256"],
            configured["startup_binding_sha256"],
        )
        verify_format.expected_environment_manifest_sha256 = invalid
        expect_blocked(
            lambda verify_format=verify_format: core.verify_evidence(verify_format),
            "external environment manifest anchor",
        )
        results[f"independent_verify_{label}"] = "blocked_read_only"

    verify_fired = False

    def forge_before_verify_final_readback(
        event: str,
        _ctx: core.RuntimeContext,
    ) -> None:
        nonlocal verify_fired
        if event == "before_verify_pair_postcheck" and not verify_fired:
            verify_fired = True
            coherently_forge_environment_manifest_and_journal(
                verify_case,
                "verify-final-readback",
            )

    core._TEST_HOOK = forge_before_verify_final_readback
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(
                    verify_case,
                    configured["runtime_config_evidence_sha256"],
                    configured["startup_binding_sha256"],
                )
            ),
            "external SHA-256 anchor",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(verify_fired, "final verify manifest anchor hook did not fire")
    assert_true(
        runtime_evidence_path(verify_case).read_bytes() == pair_before["evidence"]
        and startup_binding_path(verify_case).read_bytes() == pair_before["binding"],
        "read-only verification altered the startup publication pair",
    )
    for relative, expected in configs_before.items():
        current = core.read_snapshot(
            pathlib.Path(verify_case["oni"])
            / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        )
        assert_true(current == expected, "read-only verification altered runtime configs")
    results["independent_verify_final_readback_toctou"] = "blocked_read_only"
    return results


def scenario_whatif(base: pathlib.Path) -> dict[str, str]:
    fixture = make_fixture(base, "whatif")
    before = tree_snapshot(pathlib.Path(fixture["case"]))
    result = core.configure(arguments(fixture))
    after = tree_snapshot(pathlib.Path(fixture["case"]))
    assert_true(result["status"] == "would_configure" and result["write_count"] == 0, "WhatIf result is wrong")
    assert_true(
        result.get("planned_create_only_evidence") == 2,
        "WhatIf did not plan the evidence/binding create-only pair",
    )
    policy = result.get("loader_policy") if isinstance(result.get("loader_policy"), dict) else {}
    semantics = policy.get("built_in_semantics") if isinstance(policy.get("built_in_semantics"), dict) else {}
    assembly_binding = (
        semantics.get("managed_assembly_binding")
        if isinstance(semantics.get("managed_assembly_binding"), dict)
        else {}
    )
    assert_true(
        policy.get("save_filename_key_count") == 1
        and policy.get("auto_resume_save_file_key_count") == 1
        and policy.get("save_filename_key_exact_loader") is True
        and policy.get("auto_resume_save_file_exact_loader") is True
        and policy.get("expected_one_shot_consumption") is True,
        "WhatIf loader policy is incomplete",
    )
    assert_true(
        assembly_binding.get("descriptor_read_count_per_assembly") == 2
        and [item.get("file_name") for item in assembly_binding.get("assemblies", [])]
        == list(core.MANAGED_ASSEMBLY_NAMES),
        "WhatIf loader policy lacks the descriptor-stable Managed pair",
    )
    assert_true(before == after, "WhatIf changed TestRoot bytes/mtimes or created evidence")
    return {"zero_write": "passed", "stable_plan": "passed", "exact_loader_policy": "passed"}


def scenario_preexisting_startup_pair(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for label, path_factory in (
        ("evidence", runtime_evidence_path),
        ("binding", startup_binding_path),
    ):
        fixture = make_fixture(base, f"preexisting-startup-{label}")
        original = original_config_state(fixture)
        target = path_factory(fixture)
        payload = f"external preexisting {label} must survive\n".encode("utf-8")
        target.write_bytes(payload)
        before = tree_snapshot(pathlib.Path(fixture["case"]))
        expect_blocked(
            lambda fixture=fixture: core.configure(arguments(fixture, apply=True)),
            "already exists",
        )
        assert_true(
            tree_snapshot(pathlib.Path(fixture["case"])) == before,
            f"preexisting {label} rejection performed a write",
        )
        assert_true(target.read_bytes() == payload, f"preexisting {label} was overwritten")
        for relative, expected in original.items():
            current = core.read_snapshot(
                pathlib.Path(fixture["oni"])
                / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
            )
            assert_true(current == expected, f"preexisting {label} changed {relative}")
        results[f"preexisting_{label}"] = "blocked_zero_write_external_preserved"
    return results


def scenario_testroot_isolation(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    shaped_case = make_fixture(base, "testroot-real-shaped-oni")
    shaped_root = (
        pathlib.Path(shaped_case["case"])
        / "Documents"
        / "Klei"
        / "OxygenNotIncluded"
    )
    shaped_root.mkdir(parents=True)
    shaped_args = arguments(shaped_case)
    shaped_args.oni_root = str(shaped_root)
    shaped_before = tree_snapshot(pathlib.Path(shaped_case["case"]))
    expect_blocked(
        lambda: core.configure(shaped_args),
        "exact oni-user fixture path",
    )
    assert_true(
        shaped_before == tree_snapshot(pathlib.Path(shaped_case["case"])),
        "real-shaped TestRoot ONI rejection performed a write",
    )
    results["real_shaped_oni_root"] = "blocked_zero_write"

    evidence_case = make_fixture(base, "testroot-wrong-evidence")
    alternate_evidence = pathlib.Path(evidence_case["case"]) / "alternate-evidence"
    alternate_boundary = alternate_evidence / "live-boundaries"
    alternate_boundary.mkdir(parents=True)
    evidence_args = arguments(evidence_case)
    evidence_args.evidence_root = str(alternate_evidence)
    evidence_args.boundary_report_root = str(alternate_boundary)
    expect_blocked(
        lambda: core.configure(evidence_args),
        "exact evidence fixture path",
    )
    results["alternate_evidence_root"] = "blocked"

    spoof_case = make_fixture(base, "known-folder-userprofile-spoof")
    spoof_profile = pathlib.Path(spoof_case["case"]) / "spoofed-profile"
    spoof_oni = spoof_profile / "Documents" / "Klei" / "OxygenNotIncluded"
    spoof_oni.mkdir(parents=True)
    spoof_args = arguments(spoof_case)
    spoof_args.test_root = None
    spoof_args.test_managed_assembly_csharp_sha256 = None
    spoof_args.test_managed_assembly_csharp_firstpass_sha256 = None
    spoof_args.oni_root = str(spoof_oni)
    spoof_before = tree_snapshot(pathlib.Path(spoof_case["case"]))
    original_userprofile = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = str(spoof_profile)
    try:
        expect_blocked(
            lambda: core.configure(spoof_args),
            "default ONI user-data root",
        )
    finally:
        if original_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = original_userprofile
    assert_true(
        spoof_before == tree_snapshot(pathlib.Path(spoof_case["case"])),
        "USERPROFILE spoof rejection performed a write",
    )
    results["userprofile_spoof"] = "blocked_by_known_folder_zero_write"

    controls_case = make_fixture(base, "imported-live-test-controls")
    controls_args = arguments(controls_case)
    controls_args.test_root = None
    controls_args.test_managed_assembly_csharp_sha256 = None
    controls_args.test_managed_assembly_csharp_firstpass_sha256 = None
    controls_before = tree_snapshot(pathlib.Path(controls_case["case"]))

    controls_args.test_fail_after_replace = 1
    expect_blocked(
        lambda: core.configure(controls_args),
        "test-only fault controls",
    )
    results["imported_partial_commit_fault"] = "blocked_before_live_paths"
    controls_args.test_fail_after_replace = 0

    core._TEST_HOOK = lambda _event, _ctx: None
    try:
        expect_blocked(
            lambda: core.configure(controls_args),
            "test-only fault controls",
        )
    finally:
        core._TEST_HOOK = None
    results["imported_generic_hook"] = "blocked_before_live_paths"

    core._TEST_MANAGED_ASSEMBLY_HOOK = lambda _name, _path: None
    try:
        expect_blocked(
            lambda: core.configure(controls_args),
            "test-only fault controls",
        )
    finally:
        core._TEST_MANAGED_ASSEMBLY_HOOK = None
    results["imported_managed_hook"] = "blocked_before_live_paths"

    core._TEST_FAULTS = {"post_link_raw_oserror"}
    try:
        expect_blocked(
            lambda: core.configure(controls_args),
            "test-only fault controls",
        )
    finally:
        core._TEST_FAULTS.clear()
    results["imported_fault_set"] = "blocked_before_live_paths"
    assert_true(
        controls_before == tree_snapshot(pathlib.Path(controls_case["case"])),
        "imported live test-control rejection performed a write",
    )
    return results


def scenario_managed_assembly_binding(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    mismatch_case = make_fixture(base, "managed-hash-mismatch")
    mismatch_main = pathlib.Path(mismatch_case["main_assembly"])
    mismatch_main.write_bytes(mismatch_main.read_bytes() + b"tamper")
    mismatch_before = tree_snapshot(pathlib.Path(mismatch_case["case"]))
    expect_blocked(
        lambda: core.configure(arguments(mismatch_case)),
        "fixed audited SHA-256",
    )
    assert_true(
        mismatch_before == tree_snapshot(pathlib.Path(mismatch_case["case"])),
        "managed assembly hash mismatch performed a write",
    )
    results["hash_mismatch"] = "blocked_zero_write"

    name_swap_case = make_fixture(base, "managed-name-swap")
    name_swap_main = pathlib.Path(name_swap_case["main_assembly"])
    name_swap_firstpass = pathlib.Path(name_swap_case["firstpass_assembly"])
    name_swap_temporary = name_swap_main.with_name(".managed-name-swap.tmp")
    os.replace(name_swap_main, name_swap_temporary)
    os.replace(name_swap_firstpass, name_swap_main)
    os.replace(name_swap_temporary, name_swap_firstpass)
    name_swap_before = tree_snapshot(pathlib.Path(name_swap_case["case"]))
    expect_blocked(
        lambda: core.configure(arguments(name_swap_case)),
        "fixed audited SHA-256",
    )
    assert_true(
        name_swap_before == tree_snapshot(pathlib.Path(name_swap_case["case"])),
        "managed assembly filename swap performed a write",
    )
    results["filename_swap"] = "blocked_zero_write"

    after_read_case = make_fixture(base, "managed-after-read-swap")
    after_read_main = pathlib.Path(after_read_case["main_assembly"])
    moved_original = after_read_main.with_name(".Assembly-CSharp.original")
    replacement = after_read_main.with_name(".Assembly-CSharp.replacement")
    replacement_payload = b"external managed assembly replacement\x00"
    replacement.write_bytes(replacement_payload)
    runtime_before = tree_snapshot(pathlib.Path(after_read_case["oni"]))
    evidence_before = tree_snapshot(pathlib.Path(after_read_case["evidence"]))
    swapped = {"value": False}

    def swap_after_first_read(name: str, path: pathlib.Path) -> None:
        if name != "Assembly-CSharp.dll" or swapped["value"]:
            return
        os.replace(path, moved_original)
        os.replace(replacement, path)
        swapped["value"] = True

    core._TEST_MANAGED_ASSEMBLY_HOOK = swap_after_first_read
    try:
        expect_blocked(
            lambda: core.configure(arguments(after_read_case)),
            "descriptor/name identity",
        )
    finally:
        core._TEST_MANAGED_ASSEMBLY_HOOK = None
    assert_true(swapped["value"], "managed assembly after-read swap hook did not run")
    assert_true(
        after_read_main.read_bytes() == replacement_payload
        and core.sha256_file(moved_original) == after_read_case["main_assembly_sha256"],
        "managed assembly after-read swap did not preserve both named objects",
    )
    assert_true(
        runtime_before == tree_snapshot(pathlib.Path(after_read_case["oni"]))
        and evidence_before == tree_snapshot(pathlib.Path(after_read_case["evidence"])),
        "managed assembly after-read swap changed runtime or evidence state",
    )
    results["after_first_read_name_swap"] = "blocked_external_objects_preserved"

    final_case = make_fixture(base, "managed-final-revalidation-swap")
    final_main = pathlib.Path(final_case["main_assembly"])
    final_moved = final_main.with_name(".Assembly-CSharp.final-original")
    final_replacement = final_main.with_name(".Assembly-CSharp.final-replacement")
    final_replacement.write_bytes(b"final revalidation replacement\x00")
    final_runtime_before = tree_snapshot(pathlib.Path(final_case["oni"]))
    final_evidence_before = tree_snapshot(pathlib.Path(final_case["evidence"]))

    def swap_before_final_revalidation(name: str, _path: pathlib.Path) -> None:
        if name != "__before_final_revalidation__":
            return
        os.replace(final_main, final_moved)
        os.replace(final_replacement, final_main)

    core._TEST_MANAGED_ASSEMBLY_HOOK = swap_before_final_revalidation
    try:
        expect_blocked(
            lambda: core.configure(arguments(final_case)),
            "final name/descriptor identity",
        )
    finally:
        core._TEST_MANAGED_ASSEMBLY_HOOK = None
    assert_true(
        final_runtime_before == tree_snapshot(pathlib.Path(final_case["oni"]))
        and final_evidence_before == tree_snapshot(pathlib.Path(final_case["evidence"])),
        "Managed final revalidation swap changed runtime or evidence state",
    )
    results["final_name_revalidation_swap"] = "blocked_zero_runtime_write"

    hardlink_case = make_fixture(base, "managed-hardlink")
    hardlink_main = pathlib.Path(hardlink_case["main_assembly"])
    os.link(hardlink_main, hardlink_main.with_name("Assembly-CSharp.external-link.dll"))
    hardlink_before = tree_snapshot(pathlib.Path(hardlink_case["case"]))
    expect_blocked(
        lambda: core.configure(arguments(hardlink_case)),
        "single-link",
    )
    assert_true(
        hardlink_before == tree_snapshot(pathlib.Path(hardlink_case["case"])),
        "managed assembly hardlink rejection performed a write",
    )
    results["hardlink"] = "blocked_zero_write"

    reparse_case = make_fixture(base, "managed-reparse")
    reparse_managed = pathlib.Path(reparse_case["managed"])
    real_managed = reparse_managed.with_name("Managed-real")
    reparse_managed.replace(real_managed)
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(reparse_managed), str(real_managed)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not reparse_managed.exists():
        real_managed.replace(reparse_managed)
        raise VerificationFailure("TestRoot cannot create the Managed reparse fixture")
    try:
        expect_blocked(lambda: core.configure(arguments(reparse_case)), "reparse")
    finally:
        os.rmdir(reparse_managed)
        real_managed.replace(reparse_managed)
    results["managed_directory_reparse"] = "blocked"

    wrong_dir_case = make_fixture(base, "managed-wrong-directory")
    wrong_managed = pathlib.Path(wrong_dir_case["managed"]).with_name("WrongManaged")
    wrong_managed.mkdir(parents=True)
    shutil.copy2(wrong_dir_case["main_assembly"], wrong_managed / "Assembly-CSharp.dll")
    shutil.copy2(
        wrong_dir_case["firstpass_assembly"],
        wrong_managed / "Assembly-CSharp-firstpass.dll",
    )
    wrong_dir_args = arguments(wrong_dir_case)
    wrong_dir_args.game_managed_dir = str(wrong_managed)
    expect_blocked(
        lambda: core.configure(wrong_dir_args),
        "exact fixture path",
    )
    results["wrong_managed_directory"] = "blocked"

    override_case = make_fixture(base, "managed-live-override")
    override_args = arguments(override_case)
    override_args.test_root = None
    expect_blocked(
        lambda: core.resolve_roots(override_args),
        "live managed assembly hash overrides are forbidden",
    )
    results["live_hash_override"] = "blocked_before_live_paths"

    verify_case = make_fixture(base, "managed-current-verify")
    configured = core.configure(arguments(verify_case, apply=True))
    verify_main = pathlib.Path(verify_case["main_assembly"])
    verify_main.write_bytes(verify_main.read_bytes() + b"post-config drift")
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                verify_case,
                configured["runtime_config_evidence_sha256"],
            )
        ),
        "fixed audited SHA-256",
    )
    results["independent_verify_current_drift"] = "blocked"

    precommit_case = make_fixture(base, "managed-precommit-drift")
    precommit_original = original_config_state(precommit_case)

    def drift_before_commit(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_commit":
            path = pathlib.Path(precommit_case["main_assembly"])
            path.write_bytes(path.read_bytes() + b"precommit drift")

    core._TEST_HOOK = drift_before_commit
    try:
        expect_blocked(
            lambda: core.configure(arguments(precommit_case, apply=True)),
            "fixed audited SHA-256",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(precommit_case, precommit_original)
    results["apply_precommit_recapture"] = "blocked_zero_commit"

    prepublish_case = make_fixture(base, "managed-prepublish-drift")
    prepublish_original = original_config_state(prepublish_case)

    def drift_before_evidence(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_evidence":
            path = pathlib.Path(prepublish_case["firstpass_assembly"])
            path.write_bytes(path.read_bytes() + b"prepublish drift")

    core._TEST_HOOK = drift_before_evidence
    try:
        expect_blocked(
            lambda: core.configure(arguments(prepublish_case, apply=True)),
            "fixed audited SHA-256",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(prepublish_case, prepublish_original)
    results["apply_prepublish_recapture"] = "blocked_configs_rolled_back"

    ancestor_apply_case = make_fixture(base, "managed-ancestor-apply-swap")
    ancestor_apply_original = original_config_state(ancestor_apply_case)
    ancestor_apply_ids = tuple(
        core.metadata_identity(os.lstat(pathlib.Path(ancestor_apply_case[key])))
        for key in ("main_assembly", "firstpass_assembly")
    )

    def replace_managed_parent_identity(fixture: Mapping[str, Any], suffix: str) -> None:
        managed = pathlib.Path(fixture["managed"])
        previous = managed.with_name(f"Managed-{suffix}")
        os.replace(managed, previous)
        managed.mkdir()
        for name in core.MANAGED_ASSEMBLY_NAMES:
            os.replace(previous / name, managed / name)

    def ancestor_swap_before_commit(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_commit":
            replace_managed_parent_identity(ancestor_apply_case, "previous")

    core._TEST_HOOK = ancestor_swap_before_commit
    try:
        expect_blocked(
            lambda: core.configure(arguments(ancestor_apply_case, apply=True)),
            "managed assembly binding changed",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(
        ancestor_apply_ids
        == tuple(
            core.metadata_identity(os.lstat(pathlib.Path(ancestor_apply_case[key])))
            for key in ("main_assembly", "firstpass_assembly")
        ),
        "Managed parent swap test changed child assembly identities",
    )
    assert_original_restored(ancestor_apply_case, ancestor_apply_original)
    results["apply_managed_ancestor_identity_swap"] = "blocked_zero_commit"

    ancestor_verify_case = make_fixture(base, "managed-ancestor-verify-swap")
    ancestor_configured = core.configure(arguments(ancestor_verify_case, apply=True))
    ancestor_verify_ids = tuple(
        core.metadata_identity(os.lstat(pathlib.Path(ancestor_verify_case[key])))
        for key in ("main_assembly", "firstpass_assembly")
    )
    replace_managed_parent_identity(ancestor_verify_case, "historical")
    assert_true(
        ancestor_verify_ids
        == tuple(
            core.metadata_identity(os.lstat(pathlib.Path(ancestor_verify_case[key])))
            for key in ("main_assembly", "firstpass_assembly")
        ),
        "historical Managed parent swap changed child assembly identities",
    )
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                ancestor_verify_case,
                ancestor_configured["runtime_config_evidence_sha256"],
            )
        ),
        "loader policy",
    )
    results["verify_managed_ancestor_identity_swap"] = "blocked"

    disclosure_case = make_fixture(base, "managed-path-disclosure")
    disclosure_result = core.configure(arguments(disclosure_case))
    serialized = json.dumps(disclosure_result, ensure_ascii=False)
    assert_true(
        str(disclosure_case["managed"]) not in serialized
        and str(disclosure_case["main_assembly"]) not in serialized
        and str(disclosure_case["firstpass_assembly"]) not in serialized,
        "Managed binding disclosed a raw path",
    )
    semantics = disclosure_result["loader_policy"]["built_in_semantics"]
    audit = semantics.get("game_build_audit")
    binding = semantics.get("managed_assembly_binding")
    assert_true(
        audit == {
            "source": "ecma335_constant_metadata",
            "type": "KleiVersion",
            "field": "ChangeList",
            "constant_type": "UInt32",
            "value": 744825,
            "expected_probe_game_build": "cl-744825",
            "bound_assembly_sha256": core.LIVE_MANAGED_ASSEMBLY_SHA256[
                "Assembly-CSharp.dll"
            ],
        }
        and binding.get("binding_mode") == "test_fixture"
        and binding.get("descriptor_read_count_per_assembly") == 2,
        "Managed binding lost its exact changelist audit or fixture mode",
    )
    results["identifiers_only_and_changelist_audit"] = "passed"
    return results


def scenario_managed_publication_binding(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    def install_external_assembly_name(fixture: Mapping[str, Any], label: str) -> None:
        main = pathlib.Path(fixture["main_assembly"])
        moved = main.with_name(f".Assembly-CSharp.{label}.owned")
        replacement = main.with_name(f".Assembly-CSharp.{label}.replacement")
        replacement.write_bytes(f"external assembly {label}\x00".encode("utf-8"))
        os.replace(main, moved)
        os.replace(replacement, main)

    before_case = make_fixture(base, "managed-before-publication-swap")
    before_original = original_config_state(before_case)

    def swap_before_publication(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_managed_prepublication_check":
            install_external_assembly_name(before_case, "before-publication")

    core._TEST_HOOK = swap_before_publication
    try:
        expect_blocked(
            lambda: core.configure(arguments(before_case, apply=True)),
            "publication",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(before_case, before_original)
    assert_true(
        not recovery_evidence_paths(before_case),
        "pre-publication Managed drift unexpectedly emitted recovery evidence",
    )
    results["before_publish_assembly_swap"] = "not_published_configs_rolled_back"

    after_case = make_fixture(base, "managed-after-publication-swap")
    after_original = original_config_state(after_case)

    def swap_after_publication_link(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_publication_link":
            install_external_assembly_name(after_case, "after-publication")

    core._TEST_HOOK = swap_after_publication_link
    try:
        expect_blocked(
            lambda: core.configure(arguments(after_case, apply=True)),
            "publication",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(after_case, after_original)
    assert_true(
        not recovery_evidence_paths(after_case),
        "owned post-publication revocation unexpectedly emitted recovery evidence",
    )
    results["after_publish_assembly_swap"] = "evidence_revoked_configs_rolled_back"

    for stage, fault in (
        ("before", "managed_prepublication_check_failure"),
        ("after", "managed_postpublication_check_failure"),
    ):
        fault_case = make_fixture(base, f"managed-{stage}-publication-callback-fault")
        fault_original = original_config_state(fault_case)
        core._TEST_FAULTS = {fault}
        try:
            expect_blocked(
                lambda fault_case=fault_case: core.configure(
                    arguments(fault_case, apply=True)
                ),
                "publication",
            )
        finally:
            core._TEST_FAULTS.clear()
        assert_original_restored(fault_case, fault_original)
        assert_true(
            not recovery_evidence_paths(fault_case),
            f"{stage}-publication callback fault unexpectedly emitted recovery",
        )
        results[f"{stage}_publish_callback_fault"] = "owned_cleanup_and_rollback"

    ambiguous_case = make_fixture(base, "managed-post-publication-ambiguous-replace")
    environment = pathlib.Path(ambiguous_case["environment"])
    external_source = environment / ".external-runtime-evidence-replacement"
    external_payload = b"external evidence replacement must survive\n"
    external_source.write_bytes(external_payload)

    def replace_evidence_before_postcheck(event: str, _ctx: core.RuntimeContext) -> None:
        if event != "before_managed_postpublication_check":
            return
        assert_true(
            core._TEST_PUBLICATION_PATHS is not None,
            "publication paths unavailable for ambiguous replacement fixture",
        )
        _temporary, target = core._TEST_PUBLICATION_PATHS
        os.replace(external_source, target)

    core._TEST_HOOK = replace_evidence_before_postcheck
    try:
        expect_blocked(
            lambda: core.configure(arguments(ambiguous_case, apply=True)),
            "outcome is unknown",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(
        runtime_evidence_path(ambiguous_case).read_bytes() == external_payload,
        "ambiguous evidence replacement was deleted or overwritten",
    )
    assert_runtime_configs_configured(ambiguous_case)
    recovery = assert_recovery_evidence(ambiguous_case)
    assert_true(
        recovery.get("publication_outcome") == "outcome_unknown",
        "ambiguous post-publication replacement lost outcome classification",
    )
    results["after_publish_evidence_replacement"] = (
        "external_preserved_outcome_unknown_recovery"
    )
    return results


def scenario_tamper_and_drift(base: pathlib.Path) -> dict[str, str]:
    manifest_case = make_fixture(base, "manifest-tamper")
    manifest_path = pathlib.Path(manifest_case["manifest"])
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["run_id"] = "tampered"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    expect_blocked(lambda: core.configure(arguments(manifest_case)), "manifest")

    config_case = make_fixture(base, "config-drift")
    prefs = pathlib.Path(config_case["oni"]) / "kplayerprefs.yaml"
    prefs.write_bytes(prefs.read_bytes() + b"# drift\r\n")
    expect_blocked(lambda: core.configure(arguments(config_case)), "drifted")

    bootstrap_case = make_fixture(base, "bootstrap-tamper")
    bootstrap_path = pathlib.Path(bootstrap_case["oni"]) / "ai-bridge" / "v27-runtime-bootstrap.json"
    bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    bootstrap["expected_mod_version"] = "0.6.8"
    bootstrap_path.write_bytes(core.json_document_bytes(bootstrap))
    expect_blocked(lambda: core.configure(arguments(bootstrap_case)), "run/version/bridge")

    source_case = make_fixture(base, "source-tamper")
    source = pathlib.Path(source_case["source"])
    source.write_bytes(source.read_bytes() + b"tamper")
    expect_blocked(lambda: core.configure(arguments(source_case)), "source_save content binding")

    dll_case = make_fixture(base, "dll-tamper")
    dll = pathlib.Path(dll_case["oni"]) / "mods" / "Dev" / "oni-ai-colony-agent" / "OniAiProbeMod.dll"
    dll.write_bytes(dll.read_bytes() + b"tamper")
    expect_blocked(lambda: core.configure(arguments(dll_case)), "DLL changed")

    coherent_case = make_fixture(base, "coherent-malicious-dll")
    malicious = b"production-shaped-coherent-malicious-probe\x00"
    release = pathlib.Path(coherent_case["release_dll"])
    installed = (
        pathlib.Path(coherent_case["oni"])
        / "mods"
        / "Dev"
        / "oni-ai-colony-agent"
        / "OniAiProbeMod.dll"
    )
    release.write_bytes(malicious)
    installed.write_bytes(malicious)
    malicious_hash = core.sha256_bytes(malicious)
    bootstrap_path = pathlib.Path(coherent_case["oni"]) / "ai-bridge" / "v27-runtime-bootstrap.json"
    bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    bootstrap["built_dll_sha256"] = malicious_hash
    bootstrap["installed_dll_sha256"] = malicious_hash
    bootstrap_path.write_text(json.dumps(bootstrap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expect_blocked(lambda: core.configure(arguments(coherent_case)), "external SHA-256 anchor")

    policy_case = make_fixture(base, "policy-tamper")
    policy_path = pathlib.Path(policy_case["oni"]) / "ai-bridge" / "action-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["actions_enabled"] = True
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    expect_blocked(lambda: core.configure(arguments(policy_case)), "strict disabled/unbound")

    stale_case = make_fixture(base, "stale-policy-allowlist")
    stale_path = pathlib.Path(stale_case["oni"]) / "ai-bridge" / "action-policy.json"
    stale = json.loads(stale_path.read_text(encoding="utf-8"))
    stale["allowed_save_hashes"] = [core.sha256_bytes(b"stale-save")]
    stale_path.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expect_blocked(lambda: core.configure(arguments(stale_case)), "strict disabled/unbound")
    return {
        "manifest_tamper": "blocked",
        "config_drift": "blocked",
        "runtime_version_tamper": "blocked",
        "source_tamper": "blocked",
        "probe_dll_tamper": "blocked",
        "coherent_malicious_dll_and_bootstrap": "blocked_by_external_anchor",
        "bridge_policy_tamper": "blocked",
        "stale_policy_save_allowlist": "blocked",
    }


def scenario_paths_and_reparse(base: pathlib.Path) -> dict[str, str]:
    escape_case = make_fixture(base, "path-escape")
    escaped = pathlib.Path(escape_case["case"]) / "escaped.sav"
    escaped.write_bytes(b"escape")
    bad_args = arguments(escape_case)
    bad_args.source_save = str(escaped)
    expect_blocked(lambda: core.configure(bad_args), "save path")

    manifest_case = make_fixture(base, "manifest-path")
    bad_manifest_args = arguments(manifest_case)
    bad_manifest_args.environment_manifest = str(pathlib.Path(manifest_case["pre"]))
    expect_blocked(lambda: core.configure(bad_manifest_args), "unique run path")

    relative_case = make_fixture(base, "relative-path")
    relative_args = arguments(relative_case)
    relative_args.source_save = os.path.relpath(relative_case["source"], pathlib.Path.cwd())
    expect_blocked(lambda: core.configure(relative_args), "absolute non-UNC")

    release_case = make_fixture(base, "release-path")
    alternate_release = pathlib.Path(release_case["case"]) / "alternate-release.dll"
    shutil.copy2(release_case["release_dll"], alternate_release)
    release_args = arguments(release_case)
    release_args.release_dll = str(alternate_release)
    expect_blocked(lambda: core.configure(release_args), "exact workspace/mod fixture path")

    expect_blocked(lambda: core.assert_safe_root(pathlib.Path(pathlib.Path.cwd().anchor), "root"), "filesystem root")
    expect_blocked(lambda: core.assert_safe_root(pathlib.Path(r"\\server\share"), "UNC"), "UNC")

    reparse_case = make_fixture(base, "reparse")
    mods = pathlib.Path(reparse_case["oni"]) / "mods"
    real_mods = mods.with_name("mods-real")
    mods.replace(real_mods)
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(mods), str(real_mods)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not mods.exists():
        real_mods.replace(mods)
        raise VerificationFailure("TestRoot cannot create the required reparse fixture")
    try:
        expect_blocked(lambda: core.configure(arguments(reparse_case)), "reparse")
    finally:
        # Remove the junction explicitly so TemporaryDirectory never traverses
        # or interprets it as an ordinary directory during cleanup.
        os.rmdir(mods)
        real_mods.replace(mods)
    return {
        "save_escape": "blocked",
        "manifest_escape": "blocked",
        "relative_path": "blocked",
        "release_exact_path": "blocked",
        "root_unc": "blocked",
        "reparse": "blocked",
    }


def scenario_verify_bridge_guards(base: pathlib.Path) -> dict[str, str]:
    fixture = make_fixture(base, "verify-bridge-guards")
    configured = core.configure(arguments(fixture, apply=True))
    verify_args = lambda: verification_arguments(
        fixture,
        configured["runtime_config_evidence_sha256"],
        configured["startup_binding_sha256"],
    )
    bridge = pathlib.Path(fixture["oni"]) / "ai-bridge"
    results: dict[str, str] = {}

    temporary = bridge / "unexpected.partial"
    temporary.write_bytes(b"unsafe partial bridge artifact\n")
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "temporary file",
        )
    finally:
        temporary.unlink(missing_ok=True)
    results["verify_bridge_temporary"] = "blocked"

    queued = bridge / "commands" / "pending" / "unexpected-command.json"
    queued.write_bytes(b"{}\n")
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "non-empty",
        )
    finally:
        queued.unlink(missing_ok=True)
    results["verify_bridge_nonterminal_queue"] = "blocked"

    control = bridge / "validation-controls" / "approved" / "unexpected-control.json"
    control.write_bytes(b"{}\n")
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "validation-control queue",
        )
    finally:
        control.unlink(missing_ok=True)
    results["verify_bridge_nonterminal_validation_control"] = "blocked"

    unknown_directory = bridge / "unknown-runtime-root"
    unknown_directory.mkdir()
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "unexpected static root entry",
        )
    finally:
        unknown_directory.rmdir()
    results["verify_bridge_unknown_root_directory"] = "blocked"

    reparse_target = pathlib.Path(fixture["case"]) / "bridge-reparse-target"
    reparse_target.mkdir()
    reparse = bridge / "unexpected-reparse"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(reparse), str(reparse_target)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        raise VerificationFailure("TestRoot cannot create the Bridge reparse fixture")
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "reparse",
        )
    finally:
        os.rmdir(reparse)
    results["verify_bridge_reparse"] = "blocked"

    probe = bridge.parent / "mods" / "Dev" / "oni-ai-colony-agent"
    runtime_probe = probe / "runtime"
    runtime_probe.mkdir()
    runtime_file = runtime_probe / "new-file.bin"
    runtime_file.write_bytes(b"planned runtime Probe churn\n")
    runtime_verified = core.verify_evidence(verify_args())
    assert_true(
        runtime_verified.get("status") == "passed",
        "safe runtime Probe churn did not pass historical evidence verification",
    )
    results["verify_probe_safe_runtime_extra"] = "passed"

    probe_temporary = runtime_probe / "unfinished.partial"
    probe_temporary.write_bytes(b"unsafe partial Probe artifact\n")
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "temporary node",
        )
    finally:
        probe_temporary.unlink(missing_ok=True)
    results["verify_probe_runtime_temporary"] = "blocked"

    probe_hardlink = runtime_probe / "hard-linked.bin"
    os.link(runtime_file, probe_hardlink)
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "non-exclusive file",
        )
    finally:
        probe_hardlink.unlink(missing_ok=True)
    results["verify_probe_runtime_hardlink"] = "blocked"

    probe_reparse_target = pathlib.Path(fixture["case"]) / "probe-runtime-target"
    probe_reparse_target.mkdir()
    probe_reparse = runtime_probe / "reparse"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(probe_reparse), str(probe_reparse_target)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        raise VerificationFailure("TestRoot cannot create the Probe runtime reparse fixture")
    try:
        expect_blocked(
            lambda: core.verify_evidence(verify_args()),
            "reparse",
        )
    finally:
        os.rmdir(probe_reparse)
    results["verify_probe_runtime_reparse"] = "blocked"

    special_metadata = os.stat_result(
        (core.stat.S_IFIFO, 0, 0, 1, 0, 0, 0, 0, 0, 0)
    )
    expect_blocked(
        lambda: core.classify_safe_probe_node(
            special_metadata,
            static_artifact=False,
        ),
        "non-file node",
    )
    results["verify_probe_runtime_special_node"] = "blocked_by_stat_mode"

    for artifact_name in core.RUNTIME_BOOTSTRAP_ARTIFACT_NAMES:
        drift_case = make_fixture(
            base,
            f"verify-probe-static-drift-{artifact_name.replace('.', '-')}",
        )
        drift_configured = core.configure(arguments(drift_case, apply=True))
        artifact_path = (
            pathlib.Path(drift_case["oni"])
            / "mods"
            / "Dev"
            / "oni-ai-colony-agent"
            / artifact_name
        )
        artifact_path.write_bytes(artifact_path.read_bytes() + b"\nstatic drift")
        expect_blocked(
            lambda drift_case=drift_case, drift_configured=drift_configured: core.verify_evidence(
                verification_arguments(
                    drift_case,
                    drift_configured["runtime_config_evidence_sha256"],
                    drift_configured["startup_binding_sha256"],
                )
            ),
            "runtime bootstrap",
        )
        results[f"verify_probe_static_drift_{artifact_name}"] = "blocked"
    return results


def scenario_input_hardlinks(base: pathlib.Path) -> dict[str, str]:
    cases = {
        "release_dll": lambda fixture: pathlib.Path(fixture["release_dll"]),
        "bootstrap": lambda fixture: pathlib.Path(fixture["oni"])
        / "ai-bridge"
        / "v27-runtime-bootstrap.json",
        "policy": lambda fixture: pathlib.Path(fixture["oni"])
        / "ai-bridge"
        / "action-policy.json",
        "prepared_config": lambda fixture: pathlib.Path(fixture["oni"]) / "kplayerprefs.yaml",
    }
    result: dict[str, str] = {}
    for label, select in cases.items():
        fixture = make_fixture(base, f"hardlink-{label}")
        path = select(fixture)
        alias = path.with_name(path.name + ".external-hardlink")
        os.link(path, alias)
        expect_blocked(lambda fixture=fixture: core.configure(arguments(fixture)), "link count")
        result[label] = "blocked"
    return result


def scenario_historical_verification(base: pathlib.Path) -> dict[str, str]:
    fixture = make_fixture(base, "historical-offline")
    configured = core.configure(arguments(fixture, apply=True))
    expected_evidence = configured["runtime_config_evidence_sha256"]
    frozen_evidence = json.loads(
        runtime_evidence_path(fixture).read_text(encoding="utf-8")
    )
    frozen_result = frozen_evidence["runtime"]["runtime_bootstrap_result"]
    frozen_result_payload = base64.b64decode(
        frozen_result["raw_utf8_base64"].encode("ascii"), validate=True
    )
    frozen_result_document = core.parse_json_payload(
        frozen_result_payload, "fixture frozen runtime bootstrap result"
    )
    unsigned_result = dict(frozen_result_document)
    claimed_result_hash = unsigned_result.pop("result_hash")
    assert_true(
        set(frozen_result) == core.RUNTIME_BOOTSTRAP_RESULT_EVIDENCE_KEYS
        and frozen_result_payload == core.json_document_bytes(frozen_result_document)
        and core.sha256_bytes(frozen_result_payload)
        == frozen_result["raw_sha256"]
        == frozen_result["external_expected_sha256"]
        and claimed_result_hash
        == frozen_result["result_hash"]
        == core.sha256_bytes(core.canonical_bytes(unsigned_result)),
        "frozen runtime bootstrap result was not independently self-consistent",
    )

    oni = pathlib.Path(fixture["oni"])
    prefs_path = oni / "kplayerprefs.yaml"
    configured_prefs = prefs_path.read_bytes()
    auto_resume_line = f"  AutoResumeSaveFile: {fixture['loader']}\r\n".encode("utf-8")
    assert_true(
        configured_prefs.count(auto_resume_line) == 1,
        "configured prelaunch prefs did not contain one exact AutoResumeSaveFile line",
    )
    consumed_prefs = configured_prefs.replace(auto_resume_line, b"", 1)
    assert_true(
        b"AutoResumeSaveFile" not in consumed_prefs
        and f"  SaveFilenameKey/: {fixture['loader']}\r\n".encode("utf-8") in consumed_prefs,
        "simulated built-in one-shot consumption changed the wrong loader key",
    )
    prefs_path.write_bytes(consumed_prefs)
    for relative, payload in {
        "mods/mods.json": b'{"post_run":true}\r\n',
        "mods/mods.json.bak": b"post-run mods backup bytes\r\n",
        "mods/config.json": b'{"post_run_config":true}\r\n',
    }.items():
        (oni / pathlib.Path(*pathlib.PurePosixPath(relative).parts)).write_bytes(payload)
    pathlib.Path(fixture["loader"]).write_bytes(b"post-run loader save mutation")
    pathlib.Path(fixture["loader"]).with_suffix(".png").write_bytes(b"post-run loader preview")
    (oni / "ai-bridge" / "action-policy.json").write_bytes(b'{"post_run_policy":true}\n')

    result = core.verify_evidence(
        verification_arguments(fixture, expected_evidence)
    )
    assert_true(
        result.get("status") == "passed"
        and result.get("historical_validation") is True
        and result.get("current_runtime_configs_required") is False,
        "historical verifier rejected explicitly mutable post-run state",
    )
    return {
        "frozen_result_raw_self_consistency": "passed",
        "mutable_action_policy_drift": "ignored_with_static_bridge_revalidated",
        "postrun_config_and_loader_drift": "ignored",
    }


def scenario_frozen_raw_payloads(base: pathlib.Path) -> dict[str, str]:
    raw_case = make_fixture(base, "raw-changed")
    configured = core.configure(arguments(raw_case, apply=True))
    evidence_path = runtime_evidence_path(raw_case)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    record = evidence["runtime"]["initial_disabled_policy"]
    raw_document = json.loads(base64.b64decode(record["raw_utf8_base64"]).decode("utf-8"))
    raw_document["generated_at"] = "2026-08-31T00:00:01+00:00"
    raw_payload = json.dumps(raw_document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    record["raw_utf8_base64"] = base64.b64encode(raw_payload).decode("ascii")
    record["raw_byte_length"] = len(raw_payload)
    record["raw_sha256"] = core.sha256_bytes(raw_payload)
    record["document_hash"] = core.sha256_bytes(core.canonical_bytes(raw_document))
    rewritten_hash = rewrite_evidence(evidence_path, evidence)
    expect_blocked(
        lambda: core.verify_evidence(verification_arguments(raw_case, rewritten_hash)),
        "raw JSON and embedded document diverge",
    )

    document_case = make_fixture(base, "document-changed")
    core.configure(arguments(document_case, apply=True))
    document_path = runtime_evidence_path(document_case)
    document_evidence = json.loads(document_path.read_text(encoding="utf-8"))
    document_record = document_evidence["runtime"]["initial_disabled_policy"]
    document_record["document"]["generated_at"] = "2026-08-31T00:00:02+00:00"
    document_record["document_hash"] = core.sha256_bytes(
        core.canonical_bytes(document_record["document"])
    )
    document_hash = rewrite_evidence(document_path, document_evidence)
    expect_blocked(
        lambda: core.verify_evidence(verification_arguments(document_case, document_hash)),
        "raw JSON and embedded document diverge",
    )

    crlf_case = make_fixture(base, "serialization-divergence")
    core.configure(arguments(crlf_case, apply=True))
    crlf_path = runtime_evidence_path(crlf_case)
    crlf_evidence = json.loads(crlf_path.read_text(encoding="utf-8"))
    crlf_record = crlf_evidence["runtime"]["initial_disabled_policy"]
    original_raw = base64.b64decode(crlf_record["raw_utf8_base64"])
    crlf_record["raw_utf8_base64"] = base64.b64encode(
        original_raw.replace(b"\n", b"\r\n")
    ).decode("ascii")
    crlf_hash = rewrite_evidence(crlf_path, crlf_evidence)
    expect_blocked(
        lambda: core.verify_evidence(verification_arguments(crlf_case, crlf_hash)),
        "byte length",
    )

    bootstrap_case = make_fixture(base, "bootstrap-raw-divergence")
    core.configure(arguments(bootstrap_case, apply=True))
    bootstrap_evidence_path = runtime_evidence_path(bootstrap_case)
    bootstrap_evidence = json.loads(bootstrap_evidence_path.read_text(encoding="utf-8"))
    bootstrap_record = bootstrap_evidence["runtime"]["bootstrap"]
    bootstrap_raw_document = json.loads(
        base64.b64decode(bootstrap_record["raw_utf8_base64"]).decode("utf-8")
    )
    bootstrap_raw_document["prepared_at"] = "2026-08-31T00:00:04+00:00"
    bootstrap_raw = (
        json.dumps(bootstrap_raw_document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    bootstrap_record["raw_utf8_base64"] = base64.b64encode(bootstrap_raw).decode("ascii")
    bootstrap_record["raw_byte_length"] = len(bootstrap_raw)
    bootstrap_record["raw_sha256"] = core.sha256_bytes(bootstrap_raw)
    bootstrap_record["document_hash"] = core.sha256_bytes(
        core.canonical_bytes(bootstrap_raw_document)
    )
    bootstrap_hash = rewrite_evidence(bootstrap_evidence_path, bootstrap_evidence)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(bootstrap_case, bootstrap_hash)
        ),
        "historical anchor",
    )

    anchor_case = make_fixture(base, "external-evidence-anchor")
    configured = core.configure(arguments(anchor_case, apply=True))
    anchor_path = runtime_evidence_path(anchor_case)
    anchor_value = json.loads(anchor_path.read_text(encoding="utf-8"))
    anchor_value["configured_at"] = "2026-08-31T00:00:03+00:00"
    rewrite_evidence(anchor_path, anchor_value)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(anchor_case, configured["runtime_config_evidence_sha256"])
        ),
        "external SHA-256 anchor",
    )
    return {
        "raw_changed_document_hash_synced": "blocked",
        "document_changed_raw_preserved": "blocked",
        "crlf_serialization_divergence": "blocked",
        "bootstrap_raw_changed": "blocked_by_current_external_anchor",
        "whole_evidence_external_anchor": "blocked",
    }


def scenario_lock_concurrency(base: pathlib.Path) -> dict[str, str]:
    lock_case = make_fixture(base, "lock-concurrency")
    original = original_config_state(lock_case)
    lock = pathlib.Path(lock_case["environment"]) / core.LOCK_NAME
    lock.write_bytes(b"concurrent")
    expect_blocked(lambda: core.configure(arguments(lock_case, apply=True)), "lock")
    lock.unlink()
    assert_original_restored(lock_case, original)

    drift_case = make_fixture(base, "commit-concurrency")
    mods = pathlib.Path(drift_case["oni"]) / "mods" / "mods.json"

    def drift_hook(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_commit":
            mods.write_bytes(mods.read_bytes() + b" ")

    core._TEST_HOOK = drift_hook
    try:
        expect_blocked(lambda: core.configure(arguments(drift_case, apply=True)), "concurrently")
    finally:
        core._TEST_HOOK = None
    assert_true(mods.read_bytes().endswith(b" "), "concurrent writer bytes were overwritten")

    evidence_case = make_fixture(base, "evidence-concurrency")
    evidence_original = original_config_state(evidence_case)
    evidence_path = pathlib.Path(evidence_case["environment"]) / core.EVIDENCE_NAME

    def evidence_hook(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_evidence":
            evidence_path.write_bytes(b"{}\n")

    core._TEST_HOOK = evidence_hook
    try:
        expect_blocked(lambda: core.configure(arguments(evidence_case, apply=True)), "already exists")
    finally:
        core._TEST_HOOK = None
    for relative, expected in evidence_original.items():
        current = core.read_snapshot(
            pathlib.Path(evidence_case["oni"]) / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        )
        assert_true(current == expected, "create-only race did not roll configs back")
    assert_true(evidence_path.read_bytes() == b"{}\n", "concurrent evidence was overwritten")
    return {"shared_finalize_lock": "blocked", "precommit_drift": "blocked", "create_only_race": "rolled_back"}


def scenario_publication_faults(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for fault in ("post_link_raw_oserror", "temp_unlink_failure", "post_publication_hash_failure"):
        fixture = make_fixture(base, f"publication-{fault}")
        original = original_config_state(fixture)
        core._TEST_FAULTS = {fault}
        try:
            expect_blocked(
                lambda fixture=fixture: core.configure(arguments(fixture, apply=True)),
                "publication",
            )
        finally:
            core._TEST_FAULTS.clear()
            core._TEST_HOOK = None
        assert_original_restored(fixture, original)
        assert_true(not recovery_evidence_paths(fixture), "safely revoked publication emitted recovery")
        assert_true(
            not list(pathlib.Path(fixture["environment"]).glob(".pub-*.tmp")),
            "safely revoked publication left a temporary name",
        )
        results[fault] = "revoked_then_rolled_back"

    for variant in ("target_moved", "target_replaced", "extra_hardlink"):
        fixture = make_fixture(base, f"publication-{variant}")
        environment = pathlib.Path(fixture["environment"])
        artifacts: list[pathlib.Path] = []

        def hook(event: str, _ctx: core.RuntimeContext, variant: str = variant) -> None:
            if event != "after_publication_link":
                return
            assert_true(core._TEST_PUBLICATION_PATHS is not None, "publication paths were unavailable")
            _temporary, target = core._TEST_PUBLICATION_PATHS
            if variant == "target_moved":
                moved = environment / "externally-moved-runtime-config-evidence.json"
                os.replace(target, moved)
                artifacts.append(moved)
            elif variant == "target_replaced":
                replacement = environment / "external-replacement.tmp"
                replacement.write_bytes(b"external replacement must survive\n")
                os.replace(replacement, target)
                artifacts.append(target)
            else:
                alias = environment / "external-runtime-config-hardlink.json"
                os.link(target, alias)
                artifacts.extend((target, alias))

        core._TEST_HOOK = hook
        try:
            expect_blocked(
                lambda: core.configure(arguments(fixture, apply=True)),
                "outcome is unknown",
            )
        finally:
            core._TEST_HOOK = None
            core._TEST_FAULTS.clear()
        assert_runtime_configs_configured(fixture)
        assert_recovery_evidence(fixture)
        assert_true(
            not list(environment.glob(".pub-*.tmp")),
            "outcome-unknown publication left its private temp link",
        )
        if variant == "target_replaced":
            assert_true(
                runtime_evidence_path(fixture).read_bytes() == b"external replacement must survive\n",
                "external evidence replacement was overwritten or deleted",
            )
        for artifact in dict.fromkeys(artifacts):
            artifact.unlink(missing_ok=True)
        results[variant] = "preserved_with_recovery"
    return results


def scenario_startup_pair_failures(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for stage, fault in (
        ("evidence", "evidence_post_link_raw_oserror"),
        ("binding", "binding_post_link_raw_oserror"),
        ("pair", "pair_postcheck_failure"),
    ):
        fixture = make_fixture(base, f"startup-pair-{stage}-failure")
        original = original_config_state(fixture)
        core._TEST_FAULTS = {fault}
        try:
            expect_blocked(
                lambda fixture=fixture: core.configure(arguments(fixture, apply=True)),
                "publication" if stage != "pair" else "fault",
            )
        finally:
            core._TEST_FAULTS.clear()
            core._TEST_HOOK = None
        assert_original_restored(fixture, original)
        assert_true(
            not recovery_evidence_paths(fixture),
            f"safely revoked {stage} failure emitted recovery evidence",
        )
        results[f"{stage}_publication_failure"] = "pair_revoked_configs_rolled_back"

    bridge_temp_case = make_fixture(base, "startup-pair-bridge-temp-race")
    bridge_temp_original = original_config_state(bridge_temp_case)
    bridge_temp = (
        pathlib.Path(bridge_temp_case["oni"])
        / "ai-bridge"
        / "external-race.partial"
    )

    def inject_bridge_temp(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_evidence_publication":
            bridge_temp.write_bytes(b"external bridge temp must survive\n")

    core._TEST_HOOK = inject_bridge_temp
    try:
        expect_blocked(
            lambda: core.configure(arguments(bridge_temp_case, apply=True)),
            "temporary file",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(bridge_temp_case, bridge_temp_original)
    assert_true(bridge_temp.exists(), "external Bridge temp race was deleted")
    bridge_temp.unlink()
    results["bridge_temp_race"] = "pair_revoked_configs_rolled_back_external_preserved"

    bridge_queue_case = make_fixture(base, "startup-pair-bridge-queue-race")
    bridge_queue_original = original_config_state(bridge_queue_case)
    bridge_queue = (
        pathlib.Path(bridge_queue_case["oni"])
        / "ai-bridge"
        / "commands"
        / "pending"
        / "external-race.json"
    )

    def inject_bridge_queue(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_pair_postcheck":
            bridge_queue.write_bytes(b"{}\n")

    core._TEST_HOOK = inject_bridge_queue
    try:
        expect_blocked(
            lambda: core.configure(arguments(bridge_queue_case, apply=True)),
            "non-empty",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(bridge_queue_case, bridge_queue_original)
    assert_true(bridge_queue.exists(), "external Bridge queue race was deleted")
    bridge_queue.unlink()
    results["bridge_queue_race"] = "pair_revoked_configs_rolled_back_external_preserved"

    evidence_case = make_fixture(base, "startup-pair-evidence-external-replace")
    evidence_environment = pathlib.Path(evidence_case["environment"])
    evidence_external = evidence_environment / ".external-after-evidence-publication"
    evidence_payload = b"external runtime evidence replacement must survive\n"
    evidence_external.write_bytes(evidence_payload)

    def replace_evidence_after_publication(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_evidence_publication":
            os.replace(evidence_external, runtime_evidence_path(evidence_case))

    core._TEST_HOOK = replace_evidence_after_publication
    try:
        expect_blocked(
            lambda: core.configure(arguments(evidence_case, apply=True)),
            "outcome is unknown",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(
        runtime_evidence_path(evidence_case).read_bytes() == evidence_payload,
        "post-evidence external replacement was deleted or overwritten",
    )
    assert_true(
        not startup_binding_path(evidence_case).exists(),
        "binding was published after evidence ownership was lost",
    )
    assert_runtime_configs_configured(evidence_case)
    evidence_recovery = assert_recovery_evidence(evidence_case)
    assert_true(
        evidence_recovery.get("publication_outcome") == "outcome_unknown",
        "post-evidence replacement lost outcome_unknown",
    )
    results["evidence_postpublish_external_replace"] = (
        "external_preserved_binding_not_published_recovery"
    )

    binding_case = make_fixture(base, "startup-pair-binding-external-replace")
    binding_external = pathlib.Path(binding_case["environment"]) / (
        ".external-runtime-startup-binding"
    )
    binding_payload = b"external startup binding replacement must survive\n"
    binding_external.write_bytes(binding_payload)

    def replace_binding_after_link(event: str, _ctx: core.RuntimeContext) -> None:
        if event != "binding_after_publication_link":
            return
        assert_true(
            core._TEST_PUBLICATION_PATHS is not None,
            "binding publication paths were unavailable",
        )
        _temporary, target = core._TEST_PUBLICATION_PATHS
        os.replace(binding_external, target)

    core._TEST_HOOK = replace_binding_after_link
    try:
        expect_blocked(
            lambda: core.configure(arguments(binding_case, apply=True)),
            "outcome is unknown",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(
        startup_binding_path(binding_case).read_bytes() == binding_payload,
        "post-binding external replacement was deleted or overwritten",
    )
    assert_true(
        not runtime_evidence_path(binding_case).exists(),
        "owned evidence was not revoked after binding ownership became unknown",
    )
    assert_runtime_configs_configured(binding_case)
    binding_recovery = assert_recovery_evidence(binding_case)
    assert_true(
        binding_recovery.get("publication_outcome") == "outcome_unknown",
        "post-binding replacement lost outcome_unknown",
    )
    results["binding_postpublish_external_replace"] = (
        "external_preserved_evidence_revoked_recovery"
    )
    return results


def scenario_startup_input_recapture(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    process_case = make_fixture(base, "startup-locked-process-recheck")
    process_original = original_config_state(process_case)

    def start_process_before_locked_recheck(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_locked_process_recheck":
            pathlib.Path(process_case["process"]).write_bytes(
                core.json_document_bytes(
                    {
                        "schema": "oni.process-state-fixture.v1",
                        "oxygen_not_included_count": 1,
                        "bridge_count": 0,
                    }
                )
            )

    core._TEST_HOOK = start_process_before_locked_recheck
    try:
        expect_blocked(
            lambda: core.configure(arguments(process_case, apply=True)),
            "must be stopped",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(process_case, process_original)
    results["locked_process_recheck"] = "blocked_before_config_write"

    process_final_case = make_fixture(base, "startup-final-process-recheck")
    process_final_original = original_config_state(process_final_case)

    def start_process_before_final_recheck(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_pair_postcheck":
            pathlib.Path(process_final_case["process"]).write_bytes(
                core.json_document_bytes(
                    {
                        "schema": "oni.process-state-fixture.v1",
                        "oxygen_not_included_count": 0,
                        "bridge_count": 1,
                    }
                )
            )

    core._TEST_HOOK = start_process_before_final_recheck
    try:
        expect_blocked(
            lambda: core.configure(arguments(process_final_case, apply=True)),
            "must be stopped",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(process_final_case, process_final_original)
    results["final_process_recheck"] = "pair_revoked_configs_rolled_back"

    loader_case = make_fixture(base, "startup-final-loader-recheck")
    loader_original = original_config_state(loader_case)
    loader = pathlib.Path(loader_case["loader"])
    moved_loader = loader.with_name(".loader-before-final-owned.sav")
    external_loader = loader.with_name(".loader-before-final-external.sav")
    external_loader_payload = b"external late loader replacement must survive\n"
    external_loader.write_bytes(external_loader_payload)

    def replace_loader_before_final(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_pair_postcheck":
            os.replace(loader, moved_loader)
            os.replace(external_loader, loader)

    core._TEST_HOOK = replace_loader_before_final
    try:
        expect_blocked(
            lambda: core.configure(arguments(loader_case, apply=True)),
            "loader save",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(loader_case, loader_original)
    assert_true(
        loader.read_bytes() == external_loader_payload and moved_loader.exists(),
        "late loader replacement or prior clone was not preserved",
    )
    results["final_loader_recheck"] = "pair_revoked_external_loader_preserved"

    source_case = make_fixture(base, "startup-final-source-recheck")
    source_original = original_config_state(source_case)
    source = pathlib.Path(source_case["source"])
    moved_source = source.with_name(".source-before-final-owned.sav")
    external_source = source.with_name(".source-before-final-external.sav")
    external_source_payload = b"external late source replacement must survive\n"
    external_source.write_bytes(external_source_payload)

    def replace_source_before_final(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_pair_postcheck":
            os.replace(source, moved_source)
            os.replace(external_source, source)

    core._TEST_HOOK = replace_source_before_final
    try:
        expect_blocked(
            lambda: core.configure(arguments(source_case, apply=True)),
            "source save",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(source_case, source_original)
    assert_true(
        source.read_bytes() == external_source_payload and moved_source.exists(),
        "late source replacement or original source was not preserved",
    )
    results["final_source_recheck"] = "pair_revoked_external_source_preserved"

    backup_case = make_fixture(base, "startup-final-backup-recheck")
    backup_original = original_config_state(backup_case)
    backup = (
        pathlib.Path(backup_case["environment"])
        / "backup"
        / "config"
        / "kplayerprefs.yaml"
    )
    moved_backup = backup.with_name(".kplayerprefs-before-final-owned.yaml")
    external_backup = backup.with_name(".kplayerprefs-before-final-external.yaml")
    external_backup_payload = b"external late backup replacement must survive\n"
    external_backup.write_bytes(external_backup_payload)

    def replace_backup_before_final(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_pair_postcheck":
            os.replace(backup, moved_backup)
            os.replace(external_backup, backup)

    core._TEST_HOOK = replace_backup_before_final
    try:
        expect_blocked(
            lambda: core.configure(arguments(backup_case, apply=True)),
            "config backup kplayerprefs.yaml",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(backup_case, backup_original)
    assert_true(
        backup.read_bytes() == external_backup_payload and moved_backup.exists(),
        "late backup replacement or original backup was not preserved",
    )
    results["final_backup_recheck"] = "pair_revoked_external_backup_preserved"

    bridge_case = make_fixture(base, "startup-bridge-parent-aba")
    bridge_original = original_config_state(bridge_case)
    bridge = pathlib.Path(bridge_case["oni"]) / "ai-bridge"
    moved_bridge = bridge.with_name("ai-bridge-before-binding-owned")

    def replace_bridge_parent(event: str, _ctx: core.RuntimeContext) -> None:
        if event != "after_evidence_publication":
            return
        os.replace(bridge, moved_bridge)
        bridge.mkdir()
        commands = bridge / "commands"
        for name in core.NONTERMINAL_QUEUES:
            (commands / name).mkdir(parents=True)
        (bridge / "external-marker.json").write_bytes(b"{}\n")

    core._TEST_HOOK = replace_bridge_parent
    try:
        expect_blocked(
            lambda: core.configure(arguments(bridge_case, apply=True)),
            "directory identity changed",
        )
    finally:
        core._TEST_HOOK = None
    assert_original_restored(bridge_case, bridge_original)
    assert_true(
        moved_bridge.exists() and (bridge / "external-marker.json").exists(),
        "Bridge parent ABA external state was deleted or overwritten",
    )
    results["bridge_parent_aba"] = "pair_revoked_external_directories_preserved"

    for label, relative in (
        ("bootstrap_bom", "v27-runtime-bootstrap.json"),
        ("policy_bom", "action-policy.json"),
    ):
        fixture = make_fixture(base, f"startup-{label}")
        original = original_config_state(fixture)
        target = pathlib.Path(fixture["oni"]) / "ai-bridge" / relative
        target.write_bytes(b"\xef\xbb\xbf" + target.read_bytes())
        expect_blocked(
            lambda fixture=fixture: core.configure(arguments(fixture, apply=True)),
            "BOM",
        )
        assert_original_restored(fixture, original)
        results[label] = "blocked_zero_config_write"
    return results


def scenario_startup_pair_tamper(base: pathlib.Path) -> dict[str, str]:
    fixture = make_fixture(base, "startup-pair-tamper")
    configured = core.configure(arguments(fixture, apply=True))
    evidence_path = runtime_evidence_path(fixture)
    binding_path = startup_binding_path(fixture)
    original_evidence_payload = evidence_path.read_bytes()
    original_binding_payload = binding_path.read_bytes()
    original_evidence = json.loads(original_evidence_payload.decode("utf-8"))
    original_binding = json.loads(original_binding_payload.decode("utf-8"))
    original_evidence_sha256 = core.sha256_bytes(original_evidence_payload)
    original_binding_sha256 = core.sha256_bytes(original_binding_payload)
    results: dict[str, str] = {}

    def reset_pair() -> None:
        evidence_path.write_bytes(original_evidence_payload)
        binding_path.write_bytes(original_binding_payload)

    def resign_intent(intent: dict[str, Any]) -> None:
        unsigned = dict(intent)
        unsigned.pop("document_hash", None)
        intent["document_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned))

    def synchronize_binding(
        binding: dict[str, Any],
        intent: Mapping[str, Any],
        evidence_sha256: str,
        configured_at: Any,
    ) -> None:
        binding.update(
            {
                "validation_run_id": intent.get("validation_run_id"),
                "startup_nonce": intent.get("startup_nonce"),
                "startup_intent_hash": intent.get("document_hash"),
                "runtime_config_evidence_sha256": evidence_sha256,
                "environment_manifest_sha256": intent.get(
                    "environment_manifest_sha256"
                ),
                "expected_game_build": intent.get("expected_game_build"),
                "expected_mod_version": intent.get("expected_mod_version"),
                "expected_probe_dll_sha256": intent.get(
                    "expected_probe_dll_sha256"
                ),
                "requested_loader_path_id_upper": intent.get(
                    "requested_loader_path_id_upper"
                ),
                "requested_loader_save_sha256": intent.get(
                    "requested_loader_save_sha256"
                ),
                "requested_loader_save_length": intent.get(
                    "requested_loader_save_length"
                ),
                "created_at": configured_at,
            }
        )

    def run_intent_tamper(
        label: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        resign_inner: bool = True,
    ) -> None:
        reset_pair()
        evidence = copy.deepcopy(original_evidence)
        intent = evidence["startup_intent"]
        mutate(intent)
        if resign_inner:
            resign_intent(intent)
        evidence_sha256 = rewrite_evidence(evidence_path, evidence)
        binding = copy.deepcopy(original_binding)
        synchronize_binding(
            binding,
            intent,
            evidence_sha256,
            evidence.get("configured_at"),
        )
        binding_sha256 = rewrite_binding(binding_path, binding)
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(fixture, evidence_sha256, binding_sha256)
            ),
            "startup intent",
        )
        results[label] = "blocked_after_coherent_resign"

    intent_tampers: dict[str, Callable[[dict[str, Any]], None]] = {
        "intent_schema": lambda value: value.__setitem__("schema", "wrong.intent.v1"),
        "intent_nonce_format": lambda value: value.__setitem__("startup_nonce", "startup-NOTHEX"),
        "intent_path_lower": lambda value: value.__setitem__(
            "requested_loader_path_id_lower", "sha256:" + "1" * 64
        ),
        "intent_path_upper": lambda value: value.__setitem__(
            "requested_loader_path_id_upper", "sha256:" + "2" * 64
        ),
        "intent_save_hash": lambda value: value.__setitem__(
            "requested_loader_save_sha256", "sha256:" + "3" * 64
        ),
        "intent_save_length": lambda value: value.__setitem__(
            "requested_loader_save_length", value["requested_loader_save_length"] + 1
        ),
        "intent_game_build": lambda value: value.__setitem__("expected_game_build", "cl-1"),
        "intent_mod_version": lambda value: value.__setitem__("expected_mod_version", "9.9.9"),
        "intent_probe_dll": lambda value: value.__setitem__(
            "expected_probe_dll_sha256", "sha256:" + "4" * 64
        ),
        "intent_environment": lambda value: value.__setitem__(
            "environment_manifest_sha256", "sha256:" + "5" * 64
        ),
        "intent_run_id": lambda value: value.__setitem__("validation_run_id", "wrong-run"),
        "intent_semantics": lambda value: value.__setitem__("auto_resume_semantics", "replayable"),
        "intent_extra_field": lambda value: value.__setitem__("unexpected", True),
        "intent_missing_field": lambda value: value.pop("requested_loader_path_id_lower"),
    }
    for label, mutate in intent_tampers.items():
        run_intent_tamper(label, mutate)
    run_intent_tamper(
        "intent_self_hash",
        lambda value: value.__setitem__("auto_resume_semantics", "tampered"),
        resign_inner=False,
    )

    reset_pair()
    timestamp_evidence = copy.deepcopy(original_evidence)
    timestamp_evidence["configured_at"] = "not-a-timestamp"
    timestamp_evidence_sha256 = rewrite_evidence(evidence_path, timestamp_evidence)
    timestamp_binding = copy.deepcopy(original_binding)
    timestamp_binding["runtime_config_evidence_sha256"] = timestamp_evidence_sha256
    timestamp_binding["created_at"] = timestamp_evidence["configured_at"]
    timestamp_binding_sha256 = rewrite_binding(binding_path, timestamp_binding)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                timestamp_evidence_sha256,
                timestamp_binding_sha256,
            )
        ),
        "timestamp",
    )
    results["evidence_timestamp"] = "blocked_after_coherent_resign"

    def run_binding_tamper(
        label: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        resign: bool = True,
        contains: str = "startup binding",
    ) -> None:
        reset_pair()
        binding = copy.deepcopy(original_binding)
        mutate(binding)
        if resign:
            binding_sha256 = rewrite_binding(binding_path, binding)
        else:
            payload = core.json_document_bytes(binding)
            binding_path.write_bytes(payload)
            binding_sha256 = core.sha256_bytes(payload)
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(
                    fixture,
                    original_evidence_sha256,
                    binding_sha256,
                )
            ),
            contains,
        )
        results[label] = "blocked_after_resign" if resign else "blocked_self_hash"

    binding_tampers: dict[str, Callable[[dict[str, Any]], None]] = {
        "binding_schema": lambda value: value.__setitem__("schema", "wrong.binding.v1"),
        "binding_nonce": lambda value: value.__setitem__("startup_nonce", "startup-" + "a" * 32),
        "binding_intent_hash": lambda value: value.__setitem__(
            "startup_intent_hash", "sha256:" + "6" * 64
        ),
        "binding_evidence_hash": lambda value: value.__setitem__(
            "runtime_config_evidence_sha256", "sha256:" + "7" * 64
        ),
        "binding_path_upper": lambda value: value.__setitem__(
            "requested_loader_path_id_upper", "sha256:" + "8" * 64
        ),
        "binding_save_hash": lambda value: value.__setitem__(
            "requested_loader_save_sha256", "sha256:" + "9" * 64
        ),
        "binding_save_length": lambda value: value.__setitem__(
            "requested_loader_save_length", value["requested_loader_save_length"] + 1
        ),
        "binding_game_build": lambda value: value.__setitem__("expected_game_build", "cl-2"),
        "binding_mod_version": lambda value: value.__setitem__("expected_mod_version", "8.8.8"),
        "binding_probe_dll": lambda value: value.__setitem__(
            "expected_probe_dll_sha256", "sha256:" + "a" * 64
        ),
        "binding_timestamp": lambda value: value.__setitem__("created_at", "not-a-timestamp"),
        "binding_timestamp_crosslink": lambda value: value.__setitem__(
            "created_at", "2026-08-31T00:00:00.000000+00:00"
        ),
        "binding_environment": lambda value: value.__setitem__(
            "environment_manifest_sha256", "sha256:" + "b" * 64
        ),
        "binding_run_id": lambda value: value.__setitem__("validation_run_id", "wrong-run"),
        "binding_extra_field": lambda value: value.__setitem__("unexpected", True),
        "binding_missing_field": lambda value: value.pop("requested_loader_path_id_upper"),
    }
    for label, mutate in binding_tampers.items():
        run_binding_tamper(label, mutate)
    run_binding_tamper(
        "binding_self_hash",
        lambda value: value.__setitem__("startup_nonce", "startup-" + "c" * 32),
        resign=False,
        contains="document hash",
    )

    reset_pair()
    wrong_anchor = "sha256:" + "0" * 64
    assert_true(
        wrong_anchor not in {original_evidence_sha256, original_binding_sha256},
        "external anchor mismatch fixture accidentally matched",
    )
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(fixture, original_evidence_sha256, wrong_anchor)
        ),
        "external SHA-256 anchor",
    )
    results["binding_external_anchor_mismatch"] = "blocked"
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(fixture, wrong_anchor, original_binding_sha256)
        ),
        "external SHA-256 anchor",
    )
    results["evidence_external_anchor_mismatch"] = "blocked"

    reset_pair()
    trailing_evidence_payload = original_evidence_payload + b" \n"
    evidence_path.write_bytes(trailing_evidence_payload)
    trailing_evidence_sha256 = core.sha256_bytes(trailing_evidence_payload)
    trailing_binding = copy.deepcopy(original_binding)
    trailing_binding["runtime_config_evidence_sha256"] = trailing_evidence_sha256
    trailing_binding_sha256 = rewrite_binding(binding_path, trailing_binding)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                trailing_evidence_sha256,
                trailing_binding_sha256,
            )
        ),
        "raw serialization",
    )
    results["evidence_trailing_bytes"] = "blocked_after_reanchor"

    reset_pair()
    trailing_binding_payload = original_binding_payload + b" \n"
    binding_path.write_bytes(trailing_binding_payload)
    trailing_binding_sha256 = core.sha256_bytes(trailing_binding_payload)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                original_evidence_sha256,
                trailing_binding_sha256,
            )
        ),
        "raw serialization",
    )
    results["binding_trailing_bytes"] = "blocked_after_reanchor"

    reset_pair()
    duplicate_evidence_payload = original_evidence_payload.replace(
        b"{\n",
        b'{\n  "schema": "duplicate",\n',
        1,
    )
    evidence_path.write_bytes(duplicate_evidence_payload)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                core.sha256_bytes(duplicate_evidence_payload),
                original_binding_sha256,
            )
        ),
        "duplicate",
    )
    results["evidence_duplicate_key"] = "blocked"

    reset_pair()
    duplicate_binding_payload = original_binding_payload.replace(
        b"{\n",
        b'{\n  "schema": "duplicate",\n',
        1,
    )
    binding_path.write_bytes(duplicate_binding_payload)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                original_evidence_sha256,
                core.sha256_bytes(duplicate_binding_payload),
            )
        ),
        "duplicate",
    )
    results["binding_duplicate_key"] = "blocked"

    reset_pair()
    length = original_evidence["startup_intent"]["requested_loader_save_length"]
    finite_line = f'    "requested_loader_save_length": {length},'.encode("utf-8")
    nonfinite_line = b'    "requested_loader_save_length": NaN,'
    nonfinite_evidence_payload = original_evidence_payload.replace(
        finite_line,
        nonfinite_line,
        1,
    )
    assert_true(
        nonfinite_evidence_payload != original_evidence_payload,
        "non-finite evidence fixture did not alter the requested length",
    )
    evidence_path.write_bytes(nonfinite_evidence_payload)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                core.sha256_bytes(nonfinite_evidence_payload),
                original_binding_sha256,
            )
        ),
        "non-finite",
    )
    results["evidence_nonfinite"] = "blocked"

    reset_pair()
    binding_length = original_binding["requested_loader_save_length"]
    binding_finite_line = f'  "requested_loader_save_length": {binding_length},'.encode(
        "utf-8"
    )
    nonfinite_binding_payload = original_binding_payload.replace(
        binding_finite_line,
        b'  "requested_loader_save_length": NaN,',
        1,
    )
    assert_true(
        nonfinite_binding_payload != original_binding_payload,
        "non-finite binding fixture did not alter the requested length",
    )
    binding_path.write_bytes(nonfinite_binding_payload)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                original_evidence_sha256,
                core.sha256_bytes(nonfinite_binding_payload),
            )
        ),
        "non-finite",
    )
    results["binding_nonfinite"] = "blocked"

    for label, target in (
        ("evidence_nlink", evidence_path),
        ("binding_nlink", binding_path),
    ):
        reset_pair()
        alias = target.with_name(f".{label}-alias.json")
        os.link(target, alias)
        try:
            expect_blocked(
                lambda: core.verify_evidence(
                    verification_arguments(
                        fixture,
                        original_evidence_sha256,
                        original_binding_sha256,
                    )
                ),
                "hard-linked" if label == "binding_nlink" else "link count",
            )
        finally:
            alias.unlink(missing_ok=True)
        results[label] = "blocked"

    reset_pair()
    assert_true(
        configured["startup_binding_sha256"] == original_binding_sha256,
        "tamper fixture lost its configure binding anchor",
    )
    return results


def scenario_canonical_typed_reconstruction(base: pathlib.Path) -> dict[str, str]:
    fixture = make_fixture(
        base,
        "canonical-typed-reconstruction",
        save_payload=b"x",
    )
    configured = core.configure(arguments(fixture, apply=True))
    evidence_path = runtime_evidence_path(fixture)
    binding_path = startup_binding_path(fixture)
    original_evidence_payload = evidence_path.read_bytes()
    original_binding_payload = binding_path.read_bytes()
    original_evidence = core.parse_json_payload(
        original_evidence_payload,
        "typed fixture evidence",
    )
    original_binding = core.parse_json_payload(
        original_binding_payload,
        "typed fixture binding",
    )
    assert_true(
        original_evidence["startup_intent"]["requested_loader_save_length"] == 1
        and not isinstance(
            original_evidence["startup_intent"]["requested_loader_save_length"],
            bool,
        ),
        "typed fixture did not create an integer-one loader binding",
    )
    results: dict[str, str] = {}

    def reset_pair() -> None:
        evidence_path.write_bytes(original_evidence_payload)
        binding_path.write_bytes(original_binding_payload)

    def run_evidence_tamper(
        label: str,
        mutate: Callable[[dict[str, Any]], None],
        contains: str,
    ) -> None:
        reset_pair()
        evidence = copy.deepcopy(original_evidence)
        mutate(evidence)
        evidence_sha256 = rewrite_evidence(evidence_path, evidence)
        binding = copy.deepcopy(original_binding)
        binding["runtime_config_evidence_sha256"] = evidence_sha256
        binding_sha256 = rewrite_binding(binding_path, binding)
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(
                    fixture,
                    evidence_sha256,
                    binding_sha256,
                )
            ),
            contains,
        )
        results[label] = "blocked_after_coherent_resign"

    run_evidence_tamper(
        "loader_policy_int_one_to_bool_true",
        lambda value: value["loader_policy"].__setitem__(
            "save_filename_key_count",
            True,
        ),
        "loader policy",
    )
    run_evidence_tamper(
        "loader_binding_length_int_one_to_bool_true",
        lambda value: value["bindings"]["loader_save"]["state"].__setitem__(
            "length",
            True,
        ),
        "source/loader binding",
    )
    run_evidence_tamper(
        "restoration_bool_true_to_int_one",
        lambda value: value["restoration"].__setitem__(
            "original_config_snapshot_bound",
            1,
        ),
        "restoration ownership",
    )
    run_evidence_tamper(
        "mod_policy_bool_true_to_int_one",
        lambda value: value["mod_policy"].__setitem__(
            "all_other_mods_disabled",
            1,
        ),
        "mod policy",
    )

    reset_pair()
    binding = copy.deepcopy(original_binding)
    binding["requested_loader_save_length"] = True
    binding_sha256 = rewrite_binding(binding_path, binding)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                configured["runtime_config_evidence_sha256"],
                binding_sha256,
            )
        ),
        "startup binding",
    )
    results["binding_length_int_one_to_bool_true"] = "blocked_after_coherent_resign"

    reset_pair()
    evidence = copy.deepcopy(original_evidence)
    intent = evidence["startup_intent"]
    intent["requested_loader_save_length"] = True
    intent_unsigned = dict(intent)
    intent_unsigned.pop("document_hash", None)
    intent["document_hash"] = core.sha256_bytes(core.canonical_bytes(intent_unsigned))
    evidence_sha256 = rewrite_evidence(evidence_path, evidence)
    binding = copy.deepcopy(original_binding)
    binding["startup_intent_hash"] = intent["document_hash"]
    binding["runtime_config_evidence_sha256"] = evidence_sha256
    binding["requested_loader_save_length"] = True
    binding_sha256 = rewrite_binding(binding_path, binding)
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                fixture,
                evidence_sha256,
                binding_sha256,
            )
        ),
        "startup intent loader length",
    )
    results["intent_and_binding_int_one_to_bool_true"] = (
        "blocked_after_coherent_nested_resign"
    )

    reset_pair()
    return results


def scenario_verify_late_recapture(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for label, target_factory in (
        ("evidence", runtime_evidence_path),
        ("binding", startup_binding_path),
    ):
        fixture = make_fixture(base, f"verify-late-{label}-replace")
        configured = core.configure(arguments(fixture, apply=True))
        target = target_factory(fixture)
        external = pathlib.Path(fixture["environment"]) / (
            f".external-late-{label}-replacement"
        )
        external_payload = f"external late {label} replacement must survive\n".encode(
            "utf-8"
        )
        external.write_bytes(external_payload)

        def replace_late(
            event: str,
            _ctx: core.RuntimeContext,
            external: pathlib.Path = external,
            target: pathlib.Path = target,
        ) -> None:
            if event == "before_verify_pair_postcheck":
                os.replace(external, target)

        core._TEST_HOOK = replace_late
        try:
            expect_blocked(
                lambda fixture=fixture, configured=configured: core.verify_evidence(
                    verification_arguments(
                        fixture,
                        configured["runtime_config_evidence_sha256"],
                        configured["startup_binding_sha256"],
                    )
                ),
                "final verification recapture",
            )
        finally:
            core._TEST_HOOK = None
        assert_true(
            target.read_bytes() == external_payload,
            f"late {label} replacement was deleted or overwritten",
        )
        results[f"late_{label}_replacement"] = "blocked_external_preserved"

    managed_case = make_fixture(base, "verify-late-managed-replace")
    managed_configured = core.configure(arguments(managed_case, apply=True))
    main_assembly = pathlib.Path(managed_case["main_assembly"])
    moved_assembly = main_assembly.with_name(".Assembly-CSharp.verify-late-owned")
    external_assembly = main_assembly.with_name(".Assembly-CSharp.verify-late-external")
    external_payload = b"external late Managed assembly must survive\x00"
    external_assembly.write_bytes(external_payload)

    def replace_managed_late(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_verify_pair_postcheck":
            os.replace(main_assembly, moved_assembly)
            os.replace(external_assembly, main_assembly)

    core._TEST_HOOK = replace_managed_late
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(
                    managed_case,
                    managed_configured["runtime_config_evidence_sha256"],
                    managed_configured["startup_binding_sha256"],
                )
            ),
            "managed assembly",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(
        main_assembly.read_bytes() == external_payload and moved_assembly.exists(),
        "late Managed replacement or prior object was not preserved",
    )
    results["late_managed_replacement"] = "blocked_external_objects_preserved"

    source_case = make_fixture(base, "verify-late-source-replace")
    source_configured = core.configure(arguments(source_case, apply=True))
    source = pathlib.Path(source_case["source"])
    moved_source = source.with_name(".source.verify-late-owned.sav")
    external_source = source.with_name(".source.verify-late-external.sav")
    source_payload = b"external source during final verification must survive\n"
    external_source.write_bytes(source_payload)

    def replace_source_late(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_verify_pair_postcheck":
            os.replace(source, moved_source)
            os.replace(external_source, source)

    core._TEST_HOOK = replace_source_late
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(
                    source_case,
                    source_configured["runtime_config_evidence_sha256"],
                    source_configured["startup_binding_sha256"],
                )
            ),
            "source save",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(
        source.read_bytes() == source_payload and moved_source.exists(),
        "late verification source replacement or original source was not preserved",
    )
    results["late_source_replacement"] = "blocked_external_objects_preserved"

    backup_case = make_fixture(base, "verify-late-backup-replace")
    backup_configured = core.configure(arguments(backup_case, apply=True))
    backup = (
        pathlib.Path(backup_case["environment"])
        / "backup"
        / "config"
        / "kplayerprefs.yaml"
    )
    moved_backup = backup.with_name(".kplayerprefs.verify-late-owned.yaml")
    external_backup = backup.with_name(".kplayerprefs.verify-late-external.yaml")
    backup_payload = b"external backup during final verification must survive\n"
    external_backup.write_bytes(backup_payload)

    def replace_backup_late(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_verify_pair_postcheck":
            os.replace(backup, moved_backup)
            os.replace(external_backup, backup)

    core._TEST_HOOK = replace_backup_late
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(
                    backup_case,
                    backup_configured["runtime_config_evidence_sha256"],
                    backup_configured["startup_binding_sha256"],
                )
            ),
            "config backup kplayerprefs.yaml",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(
        backup.read_bytes() == backup_payload and moved_backup.exists(),
        "late verification backup replacement or original backup was not preserved",
    )
    results["late_backup_replacement"] = "blocked_external_objects_preserved"

    process_case = make_fixture(base, "verify-late-process-start")
    process_configured = core.configure(arguments(process_case, apply=True))

    def start_process_late(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_verify_pair_postcheck":
            pathlib.Path(process_case["process"]).write_bytes(
                core.json_document_bytes(
                    {
                        "schema": "oni.process-state-fixture.v1",
                        "oxygen_not_included_count": 0,
                        "bridge_count": 1,
                    }
                )
            )

    core._TEST_HOOK = start_process_late
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                verification_arguments(
                    process_case,
                    process_configured["runtime_config_evidence_sha256"],
                    process_configured["startup_binding_sha256"],
                )
            ),
            "must be stopped",
        )
    finally:
        core._TEST_HOOK = None
    results["late_process_start"] = "blocked_at_final_recapture"
    return results


def scenario_descriptor_delete_guards(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for label, event, fault in (
        ("publication_target_revoke", "after_publication_revoke_check", "post_link_raw_oserror"),
        ("publication_temp_unlink", "after_publication_temp_unlink_check", None),
        ("staged_publish_cleanup", "after_staged_cleanup_check", None),
    ):
        fixture = make_fixture(base, f"delete-guard-{label}")
        environment = pathlib.Path(fixture["environment"])
        replacement = environment / f".{label}-external.tmp"
        replacement_bytes = f"external replacement for {label}\n".encode("utf-8")
        replacement.write_bytes(replacement_bytes)
        denied = {"count": 0}

        def guarded_swap(
            hook_event: str,
            _ctx: core.RuntimeContext,
            expected_event: str = event,
        ) -> None:
            if hook_event != expected_event:
                return
            assert_true(core._TEST_ATOMIC_UNLINK_PATH is not None, "atomic unlink path unavailable")
            try:
                os.replace(replacement, core._TEST_ATOMIC_UNLINK_PATH)
            except OSError:
                denied["count"] += 1

        original = original_config_state(fixture)
        core._TEST_HOOK = guarded_swap
        if fault is not None:
            core._TEST_FAULTS = {fault}
        try:
            if fault is not None:
                expect_blocked(
                    lambda: core.configure(arguments(fixture, apply=True)),
                    "publication",
                )
                assert_original_restored(fixture, original)
            else:
                configured = core.configure(arguments(fixture, apply=True))
                assert_true(configured["status"] == "configured", "guarded delete apply failed")
        finally:
            core._TEST_HOOK = None
            core._TEST_FAULTS.clear()
        assert_true(denied["count"] >= 1, f"{label} name swap was not denied")
        assert_true(replacement.read_bytes() == replacement_bytes, f"{label} replacement was deleted")
        results[label] = "swap_denied_external_preserved"
    return results


def scenario_lock_ownership(base: pathlib.Path) -> dict[str, str]:
    replaced_case = make_fixture(base, "lock-replaced-before-release")
    replacement_bytes = b"external lock replacement must survive\n"
    denied_before = {"value": False}

    def replace_lock(event: str, ctx: core.RuntimeContext) -> None:
        if event == "before_lock_release":
            try:
                (ctx.roots.environment_dir / core.LOCK_NAME).write_bytes(replacement_bytes)
            except OSError:
                denied_before["value"] = True

    core._TEST_HOOK = replace_lock
    try:
        first_configured = core.configure(arguments(replaced_case, apply=True))
    finally:
        core._TEST_HOOK = None
    replaced_lock = pathlib.Path(replaced_case["environment"]) / core.LOCK_NAME
    assert_true(
        first_configured["status"] == "configured"
        and denied_before["value"]
        and not replaced_lock.exists(),
        "lock descriptor did not deny a pre-release token overwrite",
    )
    assert_runtime_configs_configured(replaced_case)
    evidence_hash = core.file_state(runtime_evidence_path(replaced_case))["sha256"]
    assert_true(
        core.verify_evidence(verification_arguments(replaced_case, evidence_hash))["status"] == "passed",
        "lock replacement broke the already-consistent config/evidence pair",
    )

    guarded_case = make_fixture(base, "lock-after-check-guard")
    denied = {"value": False}
    replacement_source = pathlib.Path(guarded_case["environment"]) / ".guarded-replacement.tmp"
    replacement_source.write_bytes(replacement_bytes)

    def after_check_swap(event: str, ctx: core.RuntimeContext) -> None:
        if event == "after_lock_release_check":
            try:
                os.replace(replacement_source, ctx.roots.environment_dir / core.LOCK_NAME)
            except OSError:
                denied["value"] = True

    core._TEST_HOOK = after_check_swap
    try:
        configured = core.configure(arguments(guarded_case, apply=True))
    finally:
        core._TEST_HOOK = None
    assert_true(configured["status"] == "configured" and denied["value"], "lock delete guard was bypassed")
    assert_true(replacement_source.read_bytes() == replacement_bytes, "denied lock replacement was deleted")
    assert_true(
        not (pathlib.Path(guarded_case["environment"]) / core.LOCK_NAME).exists(),
        "owned lock was not released after the guarded swap attempt",
    )
    return {
        "token_payload_replacement": "denied_by_owner_descriptor",
        "after_check_swap": "denied_by_descriptor_guard",
        "owner_token_identity": "passed",
    }


def scenario_config_claim_concurrency(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    before_case = make_fixture(base, "commit-before-claim-swap")
    before_target = pathlib.Path(before_case["oni"]) / "kplayerprefs.yaml"
    before_external = b"external prefs inserted after check\r\n"

    def before_claim(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_commit_claim_1":
            replacement = before_target.with_name("external-before-claim.tmp")
            replacement.write_bytes(before_external)
            os.replace(replacement, before_target)

    core._TEST_HOOK = before_claim
    try:
        expect_blocked(
            lambda: core.configure(arguments(before_case, apply=True)),
            "external config state was preserved",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(before_target.read_bytes() == before_external, "commit-gap external bytes were overwritten")
    assert_recovery_evidence(before_case)
    results["commit_check_to_claim_swap"] = "external_preserved_with_recovery"

    gap_case = make_fixture(base, "commit-after-claim-target")
    gap_target = pathlib.Path(gap_case["oni"]) / "kplayerprefs.yaml"
    gap_external = b"external target created after atomic claim\r\n"

    def after_claim(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_commit_claim_1":
            gap_target.write_bytes(gap_external)

    core._TEST_HOOK = after_claim
    try:
        expect_blocked(
            lambda: core.configure(arguments(gap_case, apply=True)),
            "external config state was preserved",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(gap_target.read_bytes() == gap_external, "post-claim external target was overwritten")
    gap_recovery = assert_recovery_evidence(gap_case)
    assert_true(gap_recovery["recovery_claim_path_ids"], "owned original claim was not recorded")
    results["commit_claim_to_publish_target"] = "external_and_original_claim_preserved"

    drift_case = make_fixture(base, "after-replace-drift")
    drift_target = pathlib.Path(drift_case["oni"]) / "kplayerprefs.yaml"
    drift_suffix = b"external in-place drift after replace\r\n"

    def after_replace_drift(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_replace_1":
            drift_target.write_bytes(drift_target.read_bytes() + drift_suffix)

    core._TEST_HOOK = after_replace_drift
    try:
        expect_blocked(
            lambda: core.configure(arguments(drift_case, apply=True)),
            "external config state was preserved",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(drift_target.read_bytes().endswith(drift_suffix), "after-replace drift was overwritten")
    assert_recovery_evidence(drift_case)
    results["after_replace_drift"] = "preserved_with_recovery"

    swap_case = make_fixture(base, "after-replace-swap")
    swap_original = original_config_state(swap_case)
    swap_target = pathlib.Path(swap_case["oni"]) / "mods" / "mods.json"
    swap_external = b'{"external_after_replace":true}\r\n'

    def after_replace_swap(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_replace_2":
            replacement = swap_target.with_name("external-after-replace.tmp")
            replacement.write_bytes(swap_external)
            os.replace(replacement, swap_target)

    core._TEST_HOOK = after_replace_swap
    try:
        expect_blocked(
            lambda: core.configure(arguments(swap_case, apply=True)),
            "external config state was preserved",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(swap_target.read_bytes() == swap_external, "after-replace name swap was overwritten")
    assert_true(
        core.read_snapshot(pathlib.Path(swap_case["oni"]) / "kplayerprefs.yaml")
        == swap_original["kplayerprefs.yaml"],
        "owned earlier config was not rolled back after another target swapped",
    )
    assert_recovery_evidence(swap_case)
    results["after_replace_name_swap"] = "external_preserved_earlier_owned_rolled_back"

    rollback_case = make_fixture(base, "rollback-before-claim-swap")
    rollback_target = pathlib.Path(rollback_case["oni"]) / "kplayerprefs.yaml"
    rollback_external = b"external bytes in rollback check-to-claim gap\r\n"

    def rollback_gap(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "before_rollback_claim":
            replacement = rollback_target.with_name("external-rollback-gap.tmp")
            replacement.write_bytes(rollback_external)
            os.replace(replacement, rollback_target)

    args = arguments(rollback_case, apply=True)
    args.test_fail_after_replace = 1
    core._TEST_HOOK = rollback_gap
    try:
        expect_blocked(lambda: core.configure(args), "external config state was preserved")
    finally:
        core._TEST_HOOK = None
    assert_true(rollback_target.read_bytes() == rollback_external, "rollback-gap state was overwritten")
    assert_recovery_evidence(rollback_case)
    results["rollback_check_to_claim_swap"] = "external_preserved_with_recovery"

    rollback_publish_case = make_fixture(base, "rollback-after-claim-target")
    rollback_publish_target = pathlib.Path(rollback_publish_case["oni"]) / "kplayerprefs.yaml"
    rollback_publish_external = b"external target after rollback claim\r\n"

    def rollback_after_claim(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_rollback_claim":
            rollback_publish_target.write_bytes(rollback_publish_external)

    args = arguments(rollback_publish_case, apply=True)
    args.test_fail_after_replace = 1
    core._TEST_HOOK = rollback_after_claim
    try:
        expect_blocked(lambda: core.configure(args), "external config state was preserved")
    finally:
        core._TEST_HOOK = None
    assert_true(
        rollback_publish_target.read_bytes() == rollback_publish_external,
        "post-rollback-claim external target was overwritten",
    )
    publish_recovery = assert_recovery_evidence(rollback_publish_case)
    assert_true(publish_recovery["recovery_claim_path_ids"], "rollback claim was not recorded")
    results["rollback_claim_to_publish_target"] = "external_and_committed_claim_preserved"

    stage_case = make_fixture(base, "staged-close-swap")
    stage_original = original_config_state(stage_case)
    stage_external = b"external staged-name replacement must survive\r\n"
    stage_artifacts: list[pathlib.Path] = []

    def stage_swap(event: str, _ctx: core.RuntimeContext) -> None:
        if event == "after_stage_close_kplayerprefs.yaml":
            assert_true(core._TEST_STAGED_PATH is not None, "staged path was unavailable")
            staged_path = core._TEST_STAGED_PATH
            moved = staged_path.with_name(staged_path.name + ".owned-moved")
            os.replace(staged_path, moved)
            staged_path.write_bytes(stage_external)
            stage_artifacts.extend((staged_path, moved))

    core._TEST_HOOK = stage_swap
    try:
        expect_blocked(
            lambda: core.configure(arguments(stage_case, apply=True)),
            "staged runtime config name changed",
        )
    finally:
        core._TEST_HOOK = None
    for relative, expected in stage_original.items():
        assert_true(
            core.read_snapshot(
                pathlib.Path(stage_case["oni"])
                / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
            )
            == expected,
            "staging-name race changed a runtime config",
        )
    assert_true(stage_artifacts[0].read_bytes() == stage_external, "staged replacement was deleted")
    for artifact in stage_artifacts:
        artifact.unlink(missing_ok=True)
    results["staged_close_name_swap"] = "external_temp_preserved_configs_unchanged"
    return results


def scenario_partial_failure(base: pathlib.Path) -> dict[str, str]:
    for step in (1, 2):
        fixture = make_fixture(base, f"partial-{step}")
        original = original_config_state(fixture)
        args = arguments(fixture, apply=True)
        args.test_fail_after_replace = step
        expect_blocked(lambda: core.configure(args), "partial-commit")
        assert_original_restored(fixture, original)
    return {"after_first_replace": "rolled_back", "after_second_replace": "rolled_back", "bytes_and_mtime": "restored"}


def scenario_autoresume_pref_gate(base: pathlib.Path) -> dict[str, str]:
    def prefs(source: pathlib.Path, extra_strings: str = "") -> bytes:
        return (
            f"strings:\r\n"
            f"  InstalledLanguage: zh_klei\r\n"
            f"  SaveFilenameKey/: {source}\r\n"
            f"{extra_strings}"
            f"ints:\r\n"
            f"  SESSION_ID: 1\r\n"
            f"floats:\r\n"
            f"  Volume_Master: 1\r\n"
        ).encode("utf-8")

    def typed_prefs(source: pathlib.Path, section: str, value: str) -> bytes:
        ints_extra = f"  AutoResumeSaveFile: {value}\r\n" if section == "ints" else ""
        floats_extra = f"  AutoResumeSaveFile: {value}\r\n" if section == "floats" else ""
        return (
            f"strings:\r\n"
            f"  InstalledLanguage: zh_klei\r\n"
            f"  SaveFilenameKey/: {source}\r\n"
            f"ints:\r\n"
            f"  SESSION_ID: 1\r\n"
            f"{ints_extra}"
            f"floats:\r\n"
            f"  Volume_Master: 1\r\n"
            f"{floats_extra}"
        ).encode("utf-8")

    variants: dict[str, Callable[[pathlib.Path, pathlib.Path], bytes]] = {
        "preexisting_exact": lambda source, loader: prefs(
            source,
            f"  AutoResumeSaveFile: {loader}\r\n",
        ),
        "empty": lambda source, _loader: prefs(source, "  AutoResumeSaveFile:\r\n"),
        "duplicate": lambda source, loader: prefs(
            source,
            f"  AutoResumeSaveFile: {loader}\r\n  AutoResumeSaveFile:\r\n",
        ),
        "wrong_absolute": lambda source, loader: prefs(
            source,
            f"  AutoResumeSaveFile: {loader.with_name('wrong-loader.sav')}\r\n",
        ),
        "unc": lambda source, _loader: prefs(
            source,
            "  AutoResumeSaveFile: \\\\server\\share\\loader.sav\r\n",
        ),
        "relative": lambda source, _loader: prefs(
            source,
            "  AutoResumeSaveFile: relative-loader.sav\r\n",
        ),
        "quoted": lambda source, loader: prefs(
            source,
            f"  'AutoResumeSaveFile': {loader}\r\n",
        ),
        "nested_mapping_value": lambda source, _loader: prefs(
            source,
            "  holder:\r\n    AutoResumeSaveFile:\r\n      nested: true\r\n",
        ),
        "inline_other_mapping": lambda source, loader: prefs(
            source,
            f"  holder: {{AutoResumeSaveFile: {loader}}}\r\n",
        ),
        "ints_dictionary": lambda source, _loader: typed_prefs(source, "ints", "1"),
        "floats_dictionary": lambda source, _loader: typed_prefs(source, "floats", "1.0"),
    }
    results: dict[str, str] = {}
    for label, builder in variants.items():
        fixture = make_fixture(base, f"autoresume-{label}", prefs_builder=builder)
        before = tree_snapshot(pathlib.Path(fixture["case"]))
        expect_blocked(
            lambda fixture=fixture: core.configure(arguments(fixture, apply=True)),
            "AutoResumeSaveFile",
        )
        after = tree_snapshot(pathlib.Path(fixture["case"]))
        assert_true(before == after, f"{label} AutoResume rejection performed a write")
        results[label] = "blocked_zero_write"

    structural: dict[str, Callable[[pathlib.Path, pathlib.Path], bytes]] = {
        "nested_strings_mapping": lambda source, _loader: (
            f"root:\r\n  strings:\r\n    SaveFilenameKey/: {source}\r\n"
            f"ints:\r\n  SESSION_ID: 1\r\n"
        ).encode("utf-8"),
        "nested_save_key": lambda source, _loader: (
            f"strings:\r\n  holder:\r\n    SaveFilenameKey/: {source}\r\n"
            f"ints:\r\n  SESSION_ID: 1\r\n"
        ).encode("utf-8"),
    }
    for label, builder in structural.items():
        fixture = make_fixture(base, f"autoresume-{label}", prefs_builder=builder)
        before = tree_snapshot(pathlib.Path(fixture["case"]))
        expect_blocked(lambda fixture=fixture: core.configure(arguments(fixture, apply=True)), "strings")
        assert_true(
            before == tree_snapshot(pathlib.Path(fixture["case"])),
            f"{label} structural rejection performed a write",
        )
        results[label] = "blocked_zero_write"

    exact_case = make_fixture(base, "autoresume-indent-eol")
    source = pathlib.Path(exact_case["source"])
    loader = pathlib.Path(exact_case["loader"])
    original = f"strings:\n    SaveFilenameKey/: {source}\nints:\n    SESSION_ID: 1\n".encode(
        "utf-8"
    )
    expected = (
        f"strings:\n"
        f"    SaveFilenameKey/: {loader}\n"
        f"    AutoResumeSaveFile: {loader}\n"
        f"ints:\n"
        f"    SESSION_ID: 1\n"
    ).encode("utf-8")
    assert_true(
        core.build_kplayerprefs(original, source, loader) == expected,
        "AutoResume insertion did not preserve exact LF and four-space indentation",
    )
    results["exact_indent_and_eol"] = "passed"

    auto_line = f"    AutoResumeSaveFile: {loader}\n".encode("utf-8")
    save_line = f"    SaveFilenameKey/: {loader}\n".encode("utf-8")
    wrong_loader = loader.with_name("wrong-loader.sav")
    managed_binding = core.capture_managed_assembly_binding(
        core.resolve_roots(arguments(exact_case))
    )
    exact_value_tampers = {
        "desired_auto_empty": b"    AutoResumeSaveFile:\n",
        "desired_auto_wrong": f"    AutoResumeSaveFile: {wrong_loader}\n".encode("utf-8"),
        "desired_auto_unc": b"    AutoResumeSaveFile: \\\\server\\share\\loader.sav\n",
        "desired_auto_relative": b"    AutoResumeSaveFile: relative-loader.sav\n",
    }
    for label, replacement in exact_value_tampers.items():
        tampered = expected.replace(auto_line, replacement, 1)
        expect_blocked(
            lambda tampered=tampered: core.loader_policy_from_kplayerprefs(
                tampered,
                loader,
                managed_binding,
            ),
            "exact absolute manifest-bound loader",
        )
        results[label] = "blocked"
    tampered_save = expected.replace(
        save_line,
        f"    SaveFilenameKey/: {wrong_loader}\n".encode("utf-8"),
        1,
    )
    expect_blocked(
        lambda: core.loader_policy_from_kplayerprefs(
            tampered_save,
            loader,
            managed_binding,
        ),
        "exact absolute manifest-bound loader",
    )
    results["desired_save_wrong"] = "blocked"

    tamper_case = make_fixture(base, "autoresume-policy-tamper")
    configured = core.configure(arguments(tamper_case, apply=True))
    evidence_path = runtime_evidence_path(tamper_case)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["loader_policy"]["expected_one_shot_consumption"] = False
    rewritten_hash = rewrite_evidence(evidence_path, evidence)
    expect_blocked(
        lambda: core.verify_evidence(verification_arguments(tamper_case, rewritten_hash)),
        "loader policy",
    )
    assert_true(
        rewritten_hash != configured["runtime_config_evidence_sha256"],
        "loader-policy tamper did not replace the external evidence anchor fixture",
    )
    results["independent_policy_tamper"] = "blocked_after_resign"
    return results


def scenario_loader_and_confirmation(base: pathlib.Path) -> dict[str, str]:
    loader_case = make_fixture(base, "loader-binding")
    bad = arguments(loader_case)
    bad.loader_stem = "wrong-loader"
    expect_blocked(lambda: core.configure(bad), "named loader")

    confirm_case = make_fixture(base, "confirmation")
    expect_blocked(lambda: core.configure(arguments(confirm_case, apply=True, confirm=False)), "confirm-live-run-id")

    relative_case = make_fixture(base, "relative-loader")
    relative = arguments(relative_case)
    relative.loader_save = os.path.relpath(relative_case["loader"], pathlib.Path.cwd())
    expect_blocked(lambda: core.configure(relative), "absolute non-UNC")

    unc_case = make_fixture(base, "unc-loader")
    unc = arguments(unc_case)
    unc.loader_save = r"\\server\share\loader.sav"
    expect_blocked(lambda: core.configure(unc), "absolute non-UNC")

    nonbound_case = make_fixture(base, "nonbound-loader")
    nonbound = arguments(nonbound_case)
    nonbound_path = (
        pathlib.Path(nonbound_case["oni"])
        / "save_files"
        / "OtherColony"
        / pathlib.Path(nonbound_case["loader"]).name
    )
    nonbound_path.parent.mkdir(parents=True, exist_ok=True)
    nonbound_path.write_bytes(pathlib.Path(nonbound_case["loader"]).read_bytes())
    nonbound_path.with_suffix(".png").write_bytes(
        pathlib.Path(nonbound_case["loader"]).with_suffix(".png").read_bytes()
    )
    nonbound.loader_save = str(nonbound_path)
    expect_blocked(lambda: core.configure(nonbound), "sibling named-clone pair")
    return {
        "wrong_loader": "blocked",
        "relative_loader": "blocked",
        "unc_loader": "blocked",
        "non_manifest_bound_loader": "blocked",
        "missing_exact_confirmation": "blocked",
    }


def scenario_runtime_bootstrap_result_contract(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    cli_case = make_fixture(base, "runtime-result-cli")
    common = common_cli_arguments(cli_case)
    assert_parser_requires(
        core.build_parser(),
        without_cli_option(common, "--runtime-bootstrap-result"),
    )
    assert_parser_requires(
        core.build_parser(),
        without_cli_option(
            common, "--expected-runtime-bootstrap-result-sha256"
        ),
    )
    verify_argv = [
        "verify",
        *common,
        "--expected-runtime-config-evidence-sha256",
        core.sha256_bytes(b"fixture-evidence"),
        "--expected-startup-binding-sha256",
        core.sha256_bytes(b"fixture-binding"),
    ]
    assert_parser_requires(
        build_parser(),
        without_cli_option(verify_argv, "--runtime-bootstrap-result"),
    )
    assert_parser_requires(
        build_parser(),
        without_cli_option(
            verify_argv, "--expected-runtime-bootstrap-result-sha256"
        ),
    )
    results["configure_and_verify_required_cli_pair"] = "blocked_by_argparse"

    recovery_case = make_fixture(base, "runtime-result-old-probe-recovery")
    recovery_result = core.load_json(
        pathlib.Path(recovery_case["runtime_bootstrap_result"]),
        "old Probe recovery interoperability result",
    )
    assert_true(
        len(recovery_result) == 38
        and set(recovery_result) == core.RUNTIME_BOOTSTRAP_RESULT_KEYS,
        "old Probe recovery fixture is not the exact installed-ready 38-key result",
    )
    assert_true(
        recovery_result.get("old_probe_recovery_relative")
        == "recovery/old-probe"
        and recovery_result.get("install_commit_entry", {}).get("data", {}).get(
            "old_probe_recovery"
        )
        == "recovery/old-probe",
        "old Probe recovery fixture is not transaction-directory-relative",
    )
    assert_true(
        core.configure(arguments(recovery_case)).get("status") == "would_configure",
        "transaction-relative old Probe recovery result did not interoperate",
    )
    results["old_probe_recovery_transaction_relative_interop"] = "passed"

    recovery_stage = core.load_json(
        pathlib.Path(recovery_case["environment"])
        / "runtime-bootstrap-install-stage.json",
        "runtime tree-hash interoperability stage",
    )
    recovery_prestate = recovery_stage["install_plan"]["prestate"]
    present_snapshot = recovery_prestate["probe"]["snapshot"]
    present_identity = recovery_prestate["probe"]["identity"]
    absent_snapshot = recovery_prestate["old_bridge"]["snapshot"]
    absent_identity = recovery_prestate["old_bridge"]["identity"]
    core.validate_runtime_tree_contract(present_snapshot, present_identity, "probe")
    core.validate_runtime_tree_contract(absent_snapshot, absent_identity, "old_bridge")
    assert_true(
        set(present_snapshot)
        == {
            "exists",
            "root_last_write_ns",
            "entries",
            "file_count",
            "total_bytes",
            "tree_hash",
        }
        and set(absent_snapshot) == {"exists", "entries", "tree_hash"},
        "runtime tree-hash fixture does not match the environment producer shapes",
    )
    results["environment_snapshot_tree_hash_interop"] = "passed"

    typed_snapshot = copy.deepcopy(present_snapshot)
    typed_snapshot["root_last_write_ns"] = True
    typed_unsigned = dict(typed_snapshot)
    typed_unsigned.pop("tree_hash")
    typed_snapshot["tree_hash"] = core.sha256_bytes(
        core.canonical_bytes(typed_unsigned)
    )
    expect_blocked(
        lambda: core.validate_runtime_tree_contract(
            typed_snapshot,
            present_identity,
            "probe",
        ),
        "root mtime",
    )
    results["tree_hash_bool_int_coherent_resign"] = "blocked_read_only"

    extra_snapshot = copy.deepcopy(present_snapshot)
    extra_snapshot["unexpected"] = False
    extra_unsigned = dict(extra_snapshot)
    extra_unsigned.pop("tree_hash")
    extra_snapshot["tree_hash"] = core.sha256_bytes(
        core.canonical_bytes(extra_unsigned)
    )
    expect_blocked(
        lambda: core.validate_runtime_tree_contract(
            extra_snapshot,
            present_identity,
            "probe",
        ),
        "snapshot is not exact",
    )
    results["tree_hash_extra_field_coherent_resign"] = "blocked_read_only"

    missing_snapshot = copy.deepcopy(present_snapshot)
    missing_snapshot.pop("tree_hash")
    expect_blocked(
        lambda: core.validate_runtime_tree_contract(
            missing_snapshot,
            present_identity,
            "probe",
        ),
        "snapshot is not exact",
    )
    results["tree_hash_missing_field"] = "blocked_read_only"

    stale_hash_snapshot = copy.deepcopy(present_snapshot)
    stale_hash_snapshot["total_bytes"] += 1
    expect_blocked(
        lambda: core.validate_runtime_tree_contract(
            stale_hash_snapshot,
            present_identity,
            "probe",
        ),
        "tree hash is invalid",
    )
    results["tree_hash_payload_tamper"] = "blocked_read_only"

    absent_hash_snapshot = copy.deepcopy(absent_snapshot)
    absent_hash_snapshot["tree_hash"] = core.sha256_bytes(b"wrong-absent-tree")
    expect_blocked(
        lambda: core.validate_runtime_tree_contract(
            absent_hash_snapshot,
            absent_identity,
            "old_bridge",
        ),
        "absent old_bridge tree is not exact",
    )
    results["absent_tree_hash_tamper"] = "blocked_read_only"

    wrong_recovery_case = make_fixture(base, "runtime-result-old-probe-wrong-prefix")
    wrong_recovery_original = original_config_state(wrong_recovery_case)
    wrong_recovery_result = core.load_json(
        pathlib.Path(wrong_recovery_case["runtime_bootstrap_result"]),
        "wrong-prefix old Probe recovery result",
    )
    wrong_recovery = "runtime-bootstrap/recovery/old-probe"
    wrong_recovery_result["old_probe_recovery_relative"] = wrong_recovery
    wrong_recovery_result["install_commit_entry"]["data"][
        "old_probe_recovery"
    ] = wrong_recovery
    wrong_recovery_result["install_commit_entry_hash"] = core.sha256_bytes(
        core.canonical_bytes(wrong_recovery_result["install_commit_entry"])
    )
    wrong_candidate = rebuild_candidate_projection(wrong_recovery_result)
    wrong_recovery_result["candidate_hash"] = wrong_candidate["candidate_hash"]
    wrong_recovery_result["candidate_sha256"] = core.sha256_bytes(
        core.json_document_bytes(wrong_candidate)
    )
    wrong_recovery_case["expected_runtime_bootstrap_result_sha256"] = (
        rewrite_runtime_bootstrap_result(
            wrong_recovery_case,
            wrong_recovery_result,
        )
    )
    expect_blocked(
        lambda: core.configure(arguments(wrong_recovery_case, apply=True)),
        "old-Probe recovery projection",
    )
    assert_original_restored(wrong_recovery_case, wrong_recovery_original)
    results["old_probe_recovery_wrong_prefix"] = "blocked_zero_write"

    verify_anchor_case = make_fixture(base, "runtime-result-verify-anchor")
    verify_configured = core.configure(arguments(verify_anchor_case, apply=True))
    for label, value in (
        ("bool", True),
        ("bool_string", "True"),
        ("wrong", core.sha256_bytes(b"wrong-independent-runtime-result")),
    ):
        verify_args = verification_arguments(
            verify_anchor_case,
            verify_configured["runtime_config_evidence_sha256"],
            verify_configured["startup_binding_sha256"],
        )
        verify_args.expected_runtime_bootstrap_result_sha256 = value
        expect_blocked(
            lambda verify_args=verify_args: core.verify_evidence(verify_args),
            "runtime bootstrap result",
        )
        results[f"independent_verify_anchor_{label}"] = "blocked_read_only"

    format_case = make_fixture(base, "runtime-result-anchor-format")
    format_original = original_config_state(format_case)
    valid_hash = str(format_case["expected_runtime_bootstrap_result_sha256"])
    for label, value in (
        ("missing_prefix", valid_hash.removeprefix("sha256:")),
        ("uppercase_hex", "sha256:" + valid_hash.removeprefix("sha256:").upper()),
        ("short_hash", "sha256:" + "0" * 63),
        ("bool_value", True),
        ("bool_string", "True"),
        ("wrong_hash", core.sha256_bytes(b"wrong-runtime-result-anchor")),
    ):
        args = arguments(format_case, apply=True)
        args.expected_runtime_bootstrap_result_sha256 = value
        expect_blocked(lambda args=args: core.configure(args), "runtime bootstrap result")
        assert_original_restored(format_case, format_original)
        results[f"external_anchor_{label}"] = "blocked_zero_write"

    coherent_case = make_fixture(base, "runtime-result-coherent-resign")
    coherent_original = original_config_state(coherent_case)
    coherent_result = core.load_json(
        pathlib.Path(coherent_case["runtime_bootstrap_result"]),
        "coherently resigned runtime result",
    )
    coherent_result["shared_lock_released"] = 1
    coherent_case["expected_runtime_bootstrap_result_sha256"] = (
        rewrite_runtime_bootstrap_result(coherent_case, coherent_result)
    )
    expect_blocked(
        lambda: core.configure(arguments(coherent_case, apply=True)),
        "installed run",
    )
    assert_original_restored(coherent_case, coherent_original)
    results["coherent_result_bool_int_resign"] = "blocked_zero_write"

    bootstrap_case = make_fixture(base, "runtime-result-manual-bootstrap-resign")
    bootstrap_original = original_config_state(bootstrap_case)
    bootstrap_path = (
        pathlib.Path(bootstrap_case["oni"])
        / "ai-bridge"
        / "v27-runtime-bootstrap.json"
    )
    bootstrap_document = core.load_json(bootstrap_path, "manual bootstrap fixture")
    bootstrap_document["prepared_at"] = "2026-08-31T00:00:01+00:00"
    bootstrap_path.write_bytes(core.json_document_bytes(bootstrap_document))
    bootstrap_result = core.load_json(
        pathlib.Path(bootstrap_case["runtime_bootstrap_result"]),
        "manual bootstrap resigned result",
    )
    bootstrap_result["bootstrap_document_hash"] = core.sha256_bytes(
        core.canonical_bytes(bootstrap_document)
    )
    bootstrap_result["bootstrap_file_sha256"] = core.sha256_file(bootstrap_path)
    bootstrap_result["bridge_tree_content_hash"] = core.capture_runtime_bridge_content(
        bootstrap_path.parent
    )["content_hash"]
    bootstrap_result["install_commit_entry"]["data"][
        "bridge_tree_content_hash"
    ] = bootstrap_result["bridge_tree_content_hash"]
    bootstrap_result["install_commit_entry_hash"] = core.sha256_bytes(
        core.canonical_bytes(bootstrap_result["install_commit_entry"])
    )
    candidate = rebuild_candidate_projection(bootstrap_result)
    bootstrap_result["candidate_hash"] = candidate["candidate_hash"]
    bootstrap_result["candidate_sha256"] = core.sha256_bytes(
        core.json_document_bytes(candidate)
    )
    bootstrap_case["expected_runtime_bootstrap_result_sha256"] = (
        rewrite_runtime_bootstrap_result(bootstrap_case, bootstrap_result)
    )
    expect_blocked(
        lambda: core.configure(arguments(bootstrap_case, apply=True)),
        "bootstrap",
    )
    assert_original_restored(bootstrap_case, bootstrap_original)
    results["manual_bootstrap_result_commit_candidate_resign"] = (
        "blocked_zero_write"
    )

    copy_source = make_fixture(base, "runtime-result-copy-source")
    copy_target = make_fixture(base, "runtime-result-copy-target")
    copy_original = original_config_state(copy_target)
    copied_payload = pathlib.Path(copy_source["runtime_bootstrap_result"]).read_bytes()
    pathlib.Path(copy_target["runtime_bootstrap_result"]).write_bytes(copied_payload)
    copy_target["expected_runtime_bootstrap_result_sha256"] = core.sha256_bytes(
        copied_payload
    )
    expect_blocked(
        lambda: core.configure(arguments(copy_target, apply=True)),
        "bootstrap",
    )
    assert_original_restored(copy_target, copy_original)
    results["cross_environment_result_copy"] = "blocked_zero_write"

    pdb_case = make_fixture(base, "runtime-result-pdb-drift")
    pdb_original = original_config_state(pdb_case)
    pdb_path = (
        pathlib.Path(pdb_case["oni"])
        / "mods"
        / "Dev"
        / "oni-ai-colony-agent"
        / "OniAiProbeMod.pdb"
    )
    pdb_path.write_bytes(pdb_path.read_bytes() + b"drift")
    expect_blocked(
        lambda: core.configure(arguments(pdb_case, apply=True)),
        "artifact projection",
    )
    assert_original_restored(pdb_case, pdb_original)
    results["installed_pdb_drift"] = "blocked_zero_write"

    extra_case = make_fixture(base, "runtime-result-probe-extra")
    extra_original = original_config_state(extra_case)
    extra_path = (
        pathlib.Path(extra_case["oni"])
        / "mods"
        / "Dev"
        / "oni-ai-colony-agent"
        / "unexpected.dll"
    )
    extra_path.write_bytes(b"unexpected Probe tree entry")
    expect_blocked(
        lambda: core.configure(arguments(extra_case, apply=True)),
        "exact four-file tree",
    )
    assert_original_restored(extra_case, extra_original)
    results["installed_probe_tree_extra"] = "blocked_zero_write"

    unreleased_case = make_fixture(base, "runtime-result-lock-not-released")
    unreleased_original = original_config_state(unreleased_case)
    live_lock = pathlib.Path(unreleased_case["environment"]) / core.LOCK_NAME
    live_lock.write_bytes(b"foreign unreleased bootstrap lock\n")
    expect_blocked(
        lambda: core.configure(arguments(unreleased_case, apply=True)),
        "shared lock",
    )
    assert_original_restored(unreleased_case, unreleased_original)
    results["shared_lock_not_released"] = "blocked_zero_write"

    candidate_case = make_fixture(base, "runtime-result-ready-is-candidate")
    candidate_original = original_config_state(candidate_case)
    candidate_path = pathlib.Path(candidate_case["runtime_bootstrap_result"]).with_name(
        "result-candidate.json"
    )
    candidate_payload = candidate_path.read_bytes()
    pathlib.Path(candidate_case["runtime_bootstrap_result"]).write_bytes(
        candidate_payload
    )
    candidate_case["expected_runtime_bootstrap_result_sha256"] = core.sha256_bytes(
        candidate_payload
    )
    expect_blocked(
        lambda: core.configure(arguments(candidate_case, apply=True)),
        "field set",
    )
    assert_original_restored(candidate_case, candidate_original)
    results["candidate_used_as_ready_result"] = "blocked_zero_write"

    candidate_ignored_case = make_fixture(base, "runtime-candidate-path-tamper")
    candidate_path = pathlib.Path(
        candidate_ignored_case["runtime_bootstrap_result"]
    ).with_name("result-candidate.json")
    candidate_path.write_bytes(b"candidate path is not a trust input\n")
    plan = core.configure(arguments(candidate_ignored_case))
    assert_true(
        plan.get("status") == "would_configure",
        "consumer accidentally trusted the mutable candidate path",
    )
    results["candidate_path_not_consumed"] = "passed"

    path_case = make_fixture(base, "runtime-result-path-swap")
    path_original = original_config_state(path_case)
    alternate = pathlib.Path(path_case["runtime_bootstrap_result"]).with_name(
        "copied-result.json"
    )
    alternate.write_bytes(pathlib.Path(path_case["runtime_bootstrap_result"]).read_bytes())
    path_args = arguments(path_case, apply=True)
    path_args.runtime_bootstrap_result = str(alternate)
    expect_blocked(lambda: core.configure(path_args), "unique run path")
    assert_original_restored(path_case, path_original)
    results["noncanonical_result_path"] = "blocked_zero_write"

    static_policy_case = make_fixture(base, "runtime-static-policy-drift")
    configured = core.configure(arguments(static_policy_case, apply=True))
    static_policy_path = (
        pathlib.Path(static_policy_case["oni"])
        / "ai-bridge"
        / "validation-control-policy.json"
    )
    static_policy = core.load_json(static_policy_path, "static policy drift fixture")
    static_policy["controls_enabled"] = True
    static_policy_path.write_bytes(core.json_document_bytes(static_policy))
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                static_policy_case,
                configured["runtime_config_evidence_sha256"],
                configured["startup_binding_sha256"],
            )
        ),
        "static validation-control policy",
    )
    results["static_bridge_policy_drift"] = "blocked_read_only"

    unknown_static_case = make_fixture(base, "runtime-unknown-static-file")
    configured = core.configure(arguments(unknown_static_case, apply=True))
    unknown_static = pathlib.Path(unknown_static_case["oni"]) / "ai-bridge" / "oni_bridge.py"
    unknown_static.write_bytes(b"# unbound injected bridge code\n")
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                unknown_static_case,
                configured["runtime_config_evidence_sha256"],
                configured["startup_binding_sha256"],
            )
        ),
        "unexpected static root entry",
    )
    results["unbound_bridge_code_injection"] = "blocked_read_only"

    dynamic_case = make_fixture(base, "runtime-known-dynamic-bridge-surface")
    dynamic_configured = core.configure(arguments(dynamic_case, apply=True))
    dynamic_bridge = pathlib.Path(dynamic_case["oni"]) / "ai-bridge"
    actual_runtime_files = {
        "action-outcomes.json",
        "advice.json",
        "advice.md",
        "decision-frame.json",
        "evidence-continuity.json",
        "health.json",
        "incidents.json",
        "latest_state.json",
        "llm-health.json",
        "plan.json",
        "state.jsonl",
        "state_summary.md",
        "strategy.json",
        "strategy.md",
    }
    assert_true(
        actual_runtime_files.issubset(core.RUNTIME_DYNAMIC_BRIDGE_ROOT_FILES),
        "known Bridge runtime output fixture is not covered by the exact allowlist",
    )
    for name in actual_runtime_files:
        payload = (
            b"fixture runtime markdown\n"
            if name.endswith(".md")
            else b'{"fixture":true}\n'
        )
        (dynamic_bridge / name).write_bytes(payload)
    actual_runtime_directories = {
        "confirmed-command-evidence",
        "decision-frames",
        "sessions",
    }
    assert_true(
        actual_runtime_directories.issubset(
            core.RUNTIME_DYNAMIC_BRIDGE_ROOT_DIRECTORIES
        ),
        "known Bridge runtime directory fixture is not covered by the exact allowlist",
    )
    for name in actual_runtime_directories:
        (dynamic_bridge / name).mkdir()
    dynamic_verified = core.verify_evidence(
        verification_arguments(
            dynamic_case,
            dynamic_configured["runtime_config_evidence_sha256"],
            dynamic_configured["startup_binding_sha256"],
        )
    )
    assert_true(
        dynamic_verified.get("status") == "passed",
        "known dynamic Bridge runtime surface did not pass evidence verification",
    )
    results["known_dynamic_bridge_surface"] = "passed"

    wrong_type_case = make_fixture(base, "runtime-dynamic-root-wrong-type")
    wrong_type_configured = core.configure(arguments(wrong_type_case, apply=True))
    wrong_type_bridge = pathlib.Path(wrong_type_case["oni"]) / "ai-bridge"
    (wrong_type_bridge / "advice.json").mkdir()
    expect_blocked(
        lambda: core.verify_evidence(
            verification_arguments(
                wrong_type_case,
                wrong_type_configured["runtime_config_evidence_sha256"],
                wrong_type_configured["startup_binding_sha256"],
            )
        ),
        "dynamic root file has the wrong node type",
    )
    results["dynamic_bridge_root_wrong_node_type"] = "blocked_read_only"

    for event, expected_outcome in (
        ("after_lock_acquire", "blocked_zero_write"),
        ("after_replace_1", "owned_rollback"),
        ("before_evidence", "owned_rollback"),
        ("after_evidence_publication", "pair_revoked_owned_rollback"),
        ("before_pair_postcheck", "pair_revoked_owned_rollback"),
        ("before_lock_release", "pair_revoked_owned_rollback"),
    ):
        toctou_case = make_fixture(base, f"runtime-result-toctou-{event}")
        original = original_config_state(toctou_case)
        result_path = pathlib.Path(toctou_case["runtime_bootstrap_result"])
        moved = pathlib.Path(toctou_case["environment"]) / f".{event}-owned-result"
        replacement = pathlib.Path(toctou_case["environment"]) / (
            f".{event}-external-result"
        )
        replacement_payload = result_path.read_bytes() + b" "
        replacement.write_bytes(replacement_payload)
        swapped = False

        def swap_result(
            point: str,
            _ctx: core.RuntimeContext,
            *,
            expected_event: str = event,
        ) -> None:
            nonlocal swapped
            if point == expected_event and not swapped:
                swapped = True
                os.replace(result_path, moved)
                os.replace(replacement, result_path)

        core._TEST_HOOK = swap_result
        try:
            expect_blocked(
                lambda: core.configure(arguments(toctou_case, apply=True)),
                (
                    "final lock release"
                    if event == "before_lock_release"
                    else "external SHA-256 anchor"
                ),
            )
        finally:
            core._TEST_HOOK = None
        assert_true(swapped, f"runtime result TOCTOU hook {event} did not run")
        assert_original_restored(toctou_case, original)
        assert_true(
            result_path.read_bytes() == replacement_payload and moved.exists(),
            "runtime result TOCTOU external replacement was not preserved",
        )
        results[f"result_toctou_{event}"] = expected_outcome

    plan_case = make_fixture(base, "runtime-result-plan-apply-toctou")
    plan_original = original_config_state(plan_case)
    plan_args = arguments(plan_case)
    assert_true(
        core.configure(plan_args).get("status") == "would_configure",
        "runtime result plan fixture did not reach the read-only plan",
    )
    result_path = pathlib.Path(plan_case["runtime_bootstrap_result"])
    result_path.write_bytes(result_path.read_bytes() + b" ")
    expect_blocked(
        lambda: core.configure(arguments(plan_case, apply=True)),
        "external SHA-256 anchor",
    )
    assert_original_restored(plan_case, plan_original)
    results["result_plan_to_apply_toctou"] = "blocked_zero_write"
    return results


def scenario_parent_finalize_lock_custody(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    positive_case = make_fixture(base, "parent-finalize-lock-positive")
    positive_configured = core.configure(arguments(positive_case, apply=True))
    positive_args = verification_arguments(
        positive_case,
        positive_configured["runtime_config_evidence_sha256"],
        positive_configured["startup_binding_sha256"],
    )
    lock_path, descriptor, proof = open_parent_finalize_lock(positive_case)
    try:
        expect_blocked(
            lambda: core.verify_evidence(positive_args),
            "shared lock is still named",
        )
        results["named_parent_lock_without_proof"] = "blocked_read_only"
        verified = core.verify_evidence(
            positive_args,
            active_parent_finalize_lock=proof,
        )
        assert_true(
            verified.get("status") == "passed",
            "valid descriptor-backed parent finalize lock did not pass",
        )
        results["descriptor_backed_parent_lock"] = "passed"
        expect_blocked(
            lambda: core.validate_environment(
                positive_args,
                core.resolve_roots(positive_args),
                phase="evidence",
                allowed_config_lock_identity=core.metadata_identity(
                    os.lstat(lock_path)
                ),
                active_parent_finalize_lock=proof,
            ),
            "multiple active environment lock proofs",
        )
        results["parent_and_config_lock_proofs"] = "blocked_read_only"

        def forged_proof(**changes: Any) -> core.ActiveParentFinalizeLock:
            values = {
                "fd": proof.fd,
                "identity": proof.identity,
                "payload": proof.payload,
                "payload_sha256": proof.payload_sha256,
                "owner_token": proof.owner_token,
            }
            values.update(changes)
            return core.ActiveParentFinalizeLock(**values)

        wrong_identity = list(proof.identity)
        wrong_identity[1] += 1
        wrong_payload = core.canonical_bytes(
            {
                "schema": "oni.live-validation-finalize-lock.v1",
                "run_id": positive_case["run_id"],
                "owner_token": "1" * 32,
            }
        ) + b"\n"
        for label, forged in (
            ("wrong_identity", forged_proof(identity=tuple(wrong_identity))),
            (
                "wrong_payload",
                forged_proof(
                    payload=wrong_payload,
                    payload_sha256=core.sha256_bytes(wrong_payload),
                ),
            ),
            (
                "wrong_payload_hash",
                forged_proof(payload_sha256=core.sha256_bytes(b"wrong-lock-payload")),
            ),
            ("wrong_owner_token", forged_proof(owner_token="2" * 32)),
        ):
            expect_blocked(
                lambda forged=forged: core.verify_evidence(
                    positive_args,
                    active_parent_finalize_lock=forged,
                ),
                "active parent finalize lock",
            )
            results[f"parent_lock_{label}"] = "blocked_read_only"
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)

    descriptor_case = make_fixture(base, "parent-finalize-lock-descriptor-state")
    descriptor_configured = core.configure(arguments(descriptor_case, apply=True))
    descriptor_args = verification_arguments(
        descriptor_case,
        descriptor_configured["runtime_config_evidence_sha256"],
        descriptor_configured["startup_binding_sha256"],
    )
    lock_path, descriptor, proof = open_parent_finalize_lock(descriptor_case)
    os.close(descriptor)
    expect_blocked(
        lambda: core.verify_evidence(
            descriptor_args,
            active_parent_finalize_lock=proof,
        ),
        "descriptor is unavailable",
    )
    results["parent_lock_closed_descriptor"] = "blocked_read_only"
    foreign_descriptor_path = pathlib.Path(descriptor_case["environment"]) / (
        ".foreign-reused-descriptor"
    )
    foreign_descriptor_path.write_bytes(b"foreign descriptor target\n")
    foreign_descriptor = os.open(
        foreign_descriptor_path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )
    reused_descriptor = foreign_descriptor
    if foreign_descriptor != proof.fd:
        os.dup2(foreign_descriptor, proof.fd)
        os.close(foreign_descriptor)
        reused_descriptor = proof.fd
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                descriptor_args,
                active_parent_finalize_lock=proof,
            ),
            "descriptor custody changed",
        )
        results["parent_lock_reused_descriptor"] = "blocked_read_only"
    finally:
        os.close(reused_descriptor)
        lock_path.unlink(missing_ok=True)
        foreign_descriptor_path.unlink(missing_ok=True)

    foreign_case = make_fixture(base, "parent-finalize-lock-foreign-inode")
    foreign_configured = core.configure(arguments(foreign_case, apply=True))
    foreign_args = verification_arguments(
        foreign_case,
        foreign_configured["runtime_config_evidence_sha256"],
        foreign_configured["startup_binding_sha256"],
    )
    lock_path, descriptor, proof = open_parent_finalize_lock(foreign_case)
    moved = lock_path.with_name(".owned-parent-finalize.lock")
    os.replace(lock_path, moved)
    lock_path.write_bytes(proof.payload)
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                foreign_args,
                active_parent_finalize_lock=proof,
            ),
            "named-path custody changed",
        )
        assert_true(
            lock_path.read_bytes() == proof.payload and moved.exists(),
            "foreign same-byte lock replacement was not preserved",
        )
        results["parent_lock_same_bytes_foreign_inode"] = (
            "blocked_replacement_preserved"
        )
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)
        moved.unlink(missing_ok=True)

    mutation_case = make_fixture(base, "parent-finalize-lock-in-place-mutation")
    mutation_configured = core.configure(arguments(mutation_case, apply=True))
    mutation_args = verification_arguments(
        mutation_case,
        mutation_configured["runtime_config_evidence_sha256"],
        mutation_configured["startup_binding_sha256"],
    )
    lock_path, descriptor, proof = open_parent_finalize_lock(mutation_case)
    mutated_payload = proof.payload + b" "
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    core.write_descriptor_all(descriptor, mutated_payload)
    os.fsync(descriptor)
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                mutation_args,
                active_parent_finalize_lock=proof,
            ),
            "descriptor custody changed",
        )
        assert_true(
            lock_path.read_bytes() == mutated_payload,
            "in-place parent lock mutation was not preserved",
        )
        results["parent_lock_in_place_mutation"] = "blocked_mutation_preserved"
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)

    return_case = make_fixture(base, "parent-finalize-lock-return-swap")
    return_configured = core.configure(arguments(return_case, apply=True))
    return_args = verification_arguments(
        return_case,
        return_configured["runtime_config_evidence_sha256"],
        return_configured["startup_binding_sha256"],
    )
    lock_path, descriptor, proof = open_parent_finalize_lock(return_case)
    moved = lock_path.with_name(".owned-parent-return.lock")
    replacement = lock_path.with_name(".external-parent-return.lock")
    replacement.write_bytes(proof.payload)
    swapped = False

    def swap_before_return(event: str, _ctx: core.RuntimeContext) -> None:
        nonlocal swapped
        if event == "before_parent_finalize_lock_return_check" and not swapped:
            swapped = True
            os.replace(lock_path, moved)
            os.replace(replacement, lock_path)

    core._TEST_HOOK = swap_before_return
    try:
        expect_blocked(
            lambda: core.verify_evidence(
                return_args,
                active_parent_finalize_lock=proof,
            ),
            "named-path custody changed",
        )
    finally:
        core._TEST_HOOK = None
        os.close(descriptor)
    assert_true(swapped, "parent finalize lock final-return swap hook did not run")
    assert_true(
        lock_path.read_bytes() == proof.payload and moved.exists(),
        "parent finalize lock final-return replacement was not preserved",
    )
    results["parent_lock_final_return_swap"] = "blocked_replacement_preserved"
    lock_path.unlink(missing_ok=True)
    moved.unlink(missing_ok=True)
    replacement.unlink(missing_ok=True)
    return results


def scenario_parent_finalize_journal_append(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    def configured_verification(
        case: Mapping[str, Any],
    ) -> tuple[argparse.Namespace, dict[pathlib.Path, tuple[bytes, dict[str, Any]]]]:
        configured = core.configure(arguments(case, apply=True))
        args = verification_arguments(
            case,
            configured["runtime_config_evidence_sha256"],
            configured["startup_binding_sha256"],
        )
        publications = {
            path: core.read_snapshot(path)
            for path in (runtime_evidence_path(case), startup_binding_path(case))
        }
        return args, publications

    def assert_publications_unchanged(
        publications: Mapping[pathlib.Path, tuple[bytes, dict[str, Any]]],
    ) -> None:
        for path, expected in publications.items():
            assert_true(
                core.read_snapshot(path) == expected,
                "journal verification failure changed a frozen runtime publication",
            )

    def verify_with_parent(
        case: Mapping[str, Any],
        args: argparse.Namespace,
        *,
        blocker: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        lock_path, descriptor, proof = open_parent_finalize_lock(case)
        try:
            if blocker is not None:
                expect_blocked(
                    lambda: core.verify_evidence(
                        args,
                        active_parent_finalize_lock=proof,
                    ),
                    blocker,
                )
                return None
            return core.verify_evidence(
                args,
                active_parent_finalize_lock=proof,
            )
        finally:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    before_case = make_fixture(base, "journal-append-before-config")
    before_original = original_config_state(before_case)
    append_fixture_restore_journal(before_case)
    expect_blocked(
        lambda: core.configure(arguments(before_case, apply=True)),
        "unique environment journal origin",
    )
    assert_original_restored(before_case, before_original)
    results["configure_requires_unique_origin"] = "blocked_zero_write"

    positive_case = make_fixture(base, "journal-append-parent-positive")
    positive_args, positive_publications = configured_verification(positive_case)
    append_fixture_restore_journal(
        positive_case,
        data={"runtime_binding_sha256": "sha256:" + "4" * 64},
    )
    expect_blocked(
        lambda: core.verify_evidence(positive_args),
        "unique environment journal origin",
    )
    assert_publications_unchanged(positive_publications)
    results["standalone_evidence_requires_unique_origin"] = "blocked_read_only"
    positive = verify_with_parent(positive_case, positive_args)
    assert_true(
        isinstance(positive, dict) and positive.get("status") == "passed",
        "descriptor-backed parent proof did not authorize the canonical journal suffix",
    )
    assert_publications_unchanged(positive_publications)
    results["parent_proof_canonical_append"] = "passed_read_only"

    origin_case = make_fixture(base, "journal-origin-coherent-rechain")
    origin_args, origin_publications = configured_verification(origin_case)
    append_fixture_restore_journal(origin_case)
    origin_entries = core.verify_journal(
        pathlib.Path(origin_case["environment"]) / "restore-journal.jsonl"
    )
    first_unsigned = dict(origin_entries[0])
    first_unsigned.pop("entry_hash")
    first_data = dict(first_unsigned["data"])
    first_data["pre_boundary_sha256"] = "sha256:" + "5" * 64
    first_unsigned["data"] = first_data
    forged_first = dict(first_unsigned)
    forged_first["entry_hash"] = core.sha256_bytes(core.canonical_bytes(first_unsigned))
    second_unsigned = dict(origin_entries[1])
    second_unsigned.pop("entry_hash")
    second_unsigned["previous_entry_hash"] = forged_first["entry_hash"]
    forged_second = dict(second_unsigned)
    forged_second["entry_hash"] = core.sha256_bytes(core.canonical_bytes(second_unsigned))
    rewrite_fixture_restore_journal_in_place(
        origin_case,
        (forged_first, forged_second),
    )
    verify_with_parent(
        origin_case,
        origin_args,
        blocker="lost its external environment binding",
    )
    assert_publications_unchanged(origin_publications)
    results["coherent_origin_rechain"] = "blocked_read_only"

    prefix_case = make_fixture(base, "journal-origin-prefix-noncanonical")
    prefix_args, prefix_publications = configured_verification(prefix_case)
    append_fixture_restore_journal(prefix_case)
    prefix_path = pathlib.Path(prefix_case["environment"]) / "restore-journal.jsonl"
    with prefix_path.open("r+b") as handle:
        payload = handle.read()
        handle.seek(0)
        handle.truncate(0)
        handle.write(b" " + payload)
        handle.flush()
        os.fsync(handle.fileno())
    verify_with_parent(prefix_case, prefix_args, blocker="serialization is not exact")
    assert_publications_unchanged(prefix_publications)
    results["noncanonical_origin_prefix"] = "blocked_read_only"

    suffix_case = make_fixture(base, "journal-suffix-noncanonical")
    suffix_args, suffix_publications = configured_verification(suffix_case)
    append_fixture_restore_journal(suffix_case, canonical=False)
    verify_with_parent(suffix_case, suffix_args, blocker="serialization is not exact")
    assert_publications_unchanged(suffix_publications)
    results["noncanonical_append_serialization"] = "blocked_read_only"

    link_case = make_fixture(base, "journal-append-broken-link")
    link_args, link_publications = configured_verification(link_case)
    append_fixture_restore_journal(link_case)
    link_entries = core.verify_journal(
        pathlib.Path(link_case["environment"]) / "restore-journal.jsonl"
    )
    link_unsigned = dict(link_entries[1])
    link_unsigned.pop("entry_hash")
    link_unsigned["previous_entry_hash"] = "sha256:" + "6" * 64
    forged_link = dict(link_unsigned)
    forged_link["entry_hash"] = core.sha256_bytes(core.canonical_bytes(link_unsigned))
    rewrite_fixture_restore_journal_in_place(
        link_case,
        (link_entries[0], forged_link),
    )
    verify_with_parent(link_case, link_args, blocker="order/hash link is invalid")
    assert_publications_unchanged(link_publications)
    results["append_broken_hash_link"] = "blocked_read_only"

    tip_case = make_fixture(base, "journal-append-forged-tip")
    tip_args, tip_publications = configured_verification(tip_case)
    append_fixture_restore_journal(tip_case)
    tip_entries = core.verify_journal(
        pathlib.Path(tip_case["environment"]) / "restore-journal.jsonl"
    )
    tip_entries[1]["entry_hash"] = "sha256:" + "7" * 64
    rewrite_fixture_restore_journal_in_place(tip_case, tip_entries)
    verify_with_parent(tip_case, tip_args, blocker="hash chain is invalid")
    assert_publications_unchanged(tip_publications)
    results["forged_append_tip"] = "blocked_read_only"

    typed_case = make_fixture(base, "journal-append-bool-index")
    typed_args, typed_publications = configured_verification(typed_case)
    append_fixture_restore_journal(typed_case)
    typed_entries = core.verify_journal(
        pathlib.Path(typed_case["environment"]) / "restore-journal.jsonl"
    )
    typed_unsigned = dict(typed_entries[1])
    typed_unsigned.pop("entry_hash")
    typed_unsigned["index"] = True
    typed_entry = dict(typed_unsigned)
    typed_entry["entry_hash"] = core.sha256_bytes(core.canonical_bytes(typed_unsigned))
    rewrite_fixture_restore_journal_in_place(
        typed_case,
        (typed_entries[0], typed_entry),
    )
    verify_with_parent(typed_case, typed_args, blocker="schema is invalid")
    assert_publications_unchanged(typed_publications)
    results["append_bool_index"] = "blocked_read_only"

    identity_case = make_fixture(base, "journal-append-inode-replacement")
    identity_args, identity_publications = configured_verification(identity_case)
    append_fixture_restore_journal(identity_case)
    journal_path = pathlib.Path(identity_case["environment"]) / "restore-journal.jsonl"
    original_identity = core.node_identity_list(os.lstat(journal_path))
    replacement_path = journal_path.with_name(".foreign-restore-journal.jsonl")
    replacement_path.write_bytes(journal_path.read_bytes())
    os.replace(replacement_path, journal_path)
    assert_true(
        core.node_identity_list(os.lstat(journal_path)) != original_identity,
        "same-byte journal replacement did not change filesystem identity",
    )
    verify_with_parent(
        identity_case,
        identity_args,
        blocker="lost its external environment binding",
    )
    assert_publications_unchanged(identity_publications)
    results["same_bytes_foreign_journal_inode"] = "blocked_read_only"
    return results


def scenario_frozen_runtime_bootstrap_semantics(base: pathlib.Path) -> dict[str, str]:
    results: dict[str, str] = {}

    def invoke_helper(
        fixture: Mapping[str, Any],
        result_record: Mapping[str, Any],
        bootstrap_record: Mapping[str, Any],
        *,
        proof: Optional[core.ActiveParentFinalizeLock] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        args = arguments(fixture)
        return core.validate_frozen_runtime_bootstrap_result_semantics(
            core.resolve_roots(args),
            core.load_json(pathlib.Path(fixture["manifest"]), "fixture manifest"),
            result_record,
            bootstrap_record,
            active_parent_finalize_lock=proof,
        )

    def expect_semantic_block(
        fixture: Mapping[str, Any],
        contains: str,
        *,
        result_record: Optional[Mapping[str, Any]] = None,
        bootstrap_record: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if result_record is None or bootstrap_record is None:
            current_result, current_bootstrap = frozen_runtime_bootstrap_records(fixture)
            result_record = result_record or current_result
            bootstrap_record = bootstrap_record or current_bootstrap
        before_tree = tree_snapshot(pathlib.Path(fixture["case"]))
        producer_before = core.read_snapshot(
            PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py"
        )
        expect_blocked(
            lambda: invoke_helper(fixture, result_record, bootstrap_record),
            contains,
        )
        assert_true(
            tree_snapshot(pathlib.Path(fixture["case"])) == before_tree,
            "frozen semantic rejection changed the TestRoot tree",
        )
        assert_true(
            core.read_snapshot(
                PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py"
            )
            == producer_before,
            "frozen semantic rejection changed the bootstrap producer",
        )

    positive_case = make_fixture(base, "frozen-semantics-positive")
    append_fixture_restore_journal(positive_case)
    positive_record, positive_bootstrap = frozen_runtime_bootstrap_records(positive_case)
    before_positive = tree_snapshot(pathlib.Path(positive_case["case"]))
    reconstructed, document = invoke_helper(
        positive_case,
        positive_record,
        positive_bootstrap,
    )
    assert_true(
        core.canonical_typed_equal(reconstructed, positive_record)
        and core.canonical_typed_equal(document, positive_record["document"]),
        "historical semantic helper did not reconstruct the exact frozen record",
    )
    assert_true(
        tree_snapshot(pathlib.Path(positive_case["case"])) == before_positive,
        "historical semantic helper changed the TestRoot tree",
    )
    results["append_only_journal_without_parent"] = "passed_read_only"
    lock_path, descriptor, proof = open_parent_finalize_lock(positive_case)
    try:
        reconstructed, document = invoke_helper(
            positive_case,
            positive_record,
            positive_bootstrap,
            proof=proof,
        )
        assert_true(
            core.canonical_typed_equal(reconstructed, positive_record)
            and core.canonical_typed_equal(document, positive_record["document"]),
            "parent-custodied historical helper reconstruction diverged",
        )
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)
    results["append_only_journal_with_parent"] = "passed_read_only"

    archive_case = make_fixture(base, "frozen-semantics-archive-forgery")
    archive_result = core.load_json(
        pathlib.Path(archive_case["runtime_bootstrap_result"]),
        "fixture runtime result",
    )
    archive_result["archive_relative"] = "../foreign-archive"
    archive_result["install_commit_entry"]["data"]["archive_relative"] = (
        archive_result["archive_relative"]
    )
    archive_result["install_commit_entry_hash"] = core.sha256_bytes(
        core.canonical_bytes(archive_result["install_commit_entry"])
    )
    reseal_fixture_runtime_bootstrap_result(archive_case, archive_result)
    expect_semantic_block(archive_case, "archive_relative")
    results["coherent_archive_forgery"] = "blocked_zero_write"

    identity_case = make_fixture(base, "frozen-semantics-identity-forgery")
    identity_result = core.load_json(
        pathlib.Path(identity_case["runtime_bootstrap_result"]),
        "fixture runtime result",
    )
    identity_result["installed_probe_tree_identity"]["root_identity"][0] = True
    reseal_fixture_runtime_bootstrap_result(identity_case, identity_result)
    expect_semantic_block(identity_case, "Probe root identity")
    results["coherent_identity_forgery"] = "blocked_zero_write"

    bootstrap_case = make_fixture(base, "frozen-semantics-bootstrap-forgery")
    bootstrap_result = core.load_json(
        pathlib.Path(bootstrap_case["runtime_bootstrap_result"]),
        "fixture runtime result",
    )
    _original_result_record, bootstrap_record = frozen_runtime_bootstrap_records(
        bootstrap_case
    )
    forged_bootstrap = copy.deepcopy(bootstrap_record["document"])
    forged_bootstrap["prepared_at"] = "2026-08-31T00:00:01+00:00"
    forged_bootstrap_payload = core.json_document_bytes(forged_bootstrap)
    bootstrap_record = {
        **bootstrap_record,
        "raw_sha256": core.sha256_bytes(forged_bootstrap_payload),
        "raw_byte_length": len(forged_bootstrap_payload),
        "raw_utf8_base64": base64.b64encode(forged_bootstrap_payload).decode(
            "ascii"
        ),
        "document_hash": core.sha256_bytes(core.canonical_bytes(forged_bootstrap)),
        "document": forged_bootstrap,
    }
    bootstrap_result["bootstrap_document_hash"] = bootstrap_record["document_hash"]
    bootstrap_result["bootstrap_file_sha256"] = bootstrap_record["raw_sha256"]
    reseal_fixture_runtime_bootstrap_result(bootstrap_case, bootstrap_result)
    result_record, _unused_bootstrap = frozen_runtime_bootstrap_records(bootstrap_case)
    expect_semantic_block(
        bootstrap_case,
        "bootstrap hash/cross-binding",
        result_record=result_record,
        bootstrap_record=bootstrap_record,
    )
    results["coherent_bootstrap_forgery"] = "blocked_zero_write"

    stage_case = make_fixture(base, "frozen-semantics-stage-forgery")
    stage_path = pathlib.Path(stage_case["environment"]) / (
        "runtime-bootstrap-install-stage.json"
    )
    stage = core.load_json(stage_path, "fixture runtime stage")
    stage["transaction_path_id_lower"] = "sha256:" + "8" * 64
    unsigned_stage = dict(stage)
    unsigned_stage.pop("stage_hash", None)
    stage["stage_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned_stage))
    write_json(stage_path, stage, stamp=os.lstat(stage_path).st_mtime_ns + 10_000)
    stage_result = core.load_json(
        pathlib.Path(stage_case["runtime_bootstrap_result"]),
        "fixture runtime result",
    )
    stage_result["install_stage_hash"] = stage["stage_hash"]
    stage_result["install_stage_sha256"] = core.sha256_file(stage_path)
    reseal_fixture_runtime_bootstrap_result(stage_case, stage_result)
    expect_semantic_block(stage_case, "install stage binding")
    results["coherent_stage_forgery"] = "blocked_zero_write"

    lock_case = make_fixture(base, "frozen-semantics-lock-forgery")
    lock_result = core.load_json(
        pathlib.Path(lock_case["runtime_bootstrap_result"]),
        "fixture runtime result",
    )
    lock_claim = lock_result["install_lock_claim"]
    lock_claim["epoch"] = True
    unsigned_claim = dict(lock_claim)
    unsigned_claim.pop("claim_hash", None)
    lock_claim["claim_hash"] = core.sha256_bytes(core.canonical_bytes(unsigned_claim))
    lock_release = lock_result["install_lock_release"]
    lock_release["epoch"] = True
    lock_release["claim_hash"] = lock_claim["claim_hash"]
    unsigned_release = dict(lock_release)
    unsigned_release.pop("release_hash", None)
    lock_release["release_hash"] = core.sha256_bytes(
        core.canonical_bytes(unsigned_release)
    )
    lock_result["install_lock_epoch"] = True
    lock_result["install_lock_claim_hash"] = lock_claim["claim_hash"]
    lock_result["install_lock_release_hash"] = lock_release["release_hash"]
    reseal_fixture_runtime_bootstrap_result(lock_case, lock_result)
    expect_semantic_block(lock_case, "lock epoch")
    results["coherent_lock_forgery"] = "blocked_zero_write"

    candidate_case = make_fixture(base, "frozen-semantics-candidate-forgery")
    candidate_result = core.load_json(
        pathlib.Path(candidate_case["runtime_bootstrap_result"]),
        "fixture runtime result",
    )
    candidate_result["candidate_hash"] = "sha256:" + "9" * 64
    reseal_fixture_runtime_bootstrap_result(
        candidate_case,
        candidate_result,
        rebuild_candidate=False,
    )
    expect_semantic_block(candidate_case, "candidate reconstruction")
    results["coherent_candidate_forgery"] = "blocked_zero_write"

    producer_case = make_fixture(base, "frozen-semantics-producer-forgery")
    producer_stage_path = pathlib.Path(producer_case["environment"]) / (
        "runtime-bootstrap-install-stage.json"
    )
    producer_stage = core.load_json(producer_stage_path, "fixture runtime stage")
    forged_producer = {"length": 1, "sha256": "sha256:" + "a" * 64}
    producer_stage["producer"] = copy.deepcopy(forged_producer)
    unsigned_producer_stage = dict(producer_stage)
    unsigned_producer_stage.pop("stage_hash", None)
    producer_stage["stage_hash"] = core.sha256_bytes(
        core.canonical_bytes(unsigned_producer_stage)
    )
    write_json(
        producer_stage_path,
        producer_stage,
        stamp=os.lstat(producer_stage_path).st_mtime_ns + 10_000,
    )
    producer_result = core.load_json(
        pathlib.Path(producer_case["runtime_bootstrap_result"]),
        "fixture runtime result",
    )
    producer_result["producer"] = copy.deepcopy(forged_producer)
    producer_result["install_stage_hash"] = producer_stage["stage_hash"]
    producer_result["install_stage_sha256"] = core.sha256_file(producer_stage_path)
    reseal_fixture_runtime_bootstrap_result(producer_case, producer_result)
    expect_semantic_block(producer_case, "producer does not match frozen source")
    results["coherent_producer_forgery"] = "blocked_zero_write"

    toctou_case = make_fixture(base, "frozen-semantics-final-recheck-toctou")
    toctou_record, toctou_bootstrap = frozen_runtime_bootstrap_records(toctou_case)
    result_path = pathlib.Path(toctou_case["runtime_bootstrap_result"])
    moved_path = result_path.with_name(".owned-runtime-bootstrap-result.json")
    replacement_path = result_path.with_name(".external-runtime-bootstrap-result.json")
    replacement_payload = result_path.read_bytes() + b" "
    replacement_path.write_bytes(replacement_payload)
    swapped = False

    def swap_before_frozen_final_recheck(event: str, _ctx: Any) -> None:
        nonlocal swapped
        if event == "before_frozen_runtime_semantics_final_recheck" and not swapped:
            swapped = True
            os.replace(result_path, moved_path)
            os.replace(replacement_path, result_path)

    core._TEST_HOOK = swap_before_frozen_final_recheck
    try:
        expect_blocked(
            lambda: invoke_helper(
                toctou_case,
                toctou_record,
                toctou_bootstrap,
            ),
            "external SHA-256 anchor",
        )
    finally:
        core._TEST_HOOK = None
    assert_true(swapped, "frozen semantic final-recheck TOCTOU hook did not run")
    assert_true(
        result_path.read_bytes() == replacement_payload and moved_path.exists(),
        "frozen semantic final-recheck replacement was not preserved",
    )
    results["final_recheck_result_swap"] = "blocked_replacement_preserved"
    return results


def run_self_test(report_path: pathlib.Path) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="oni-g0-runtime-config-") as temporary:
        base = pathlib.Path(temporary)
        cases["positive"] = scenario_positive(base)
        cases["cli_surface"] = scenario_cli_surface(base)
        cases["environment_manifest_external_anchor"] = (
            scenario_environment_manifest_external_anchor(base)
        )
        cases["whatif"] = scenario_whatif(base)
        cases["preexisting_startup_pair"] = scenario_preexisting_startup_pair(base)
        cases["testroot_isolation"] = scenario_testroot_isolation(base)
        cases["managed_assembly_binding"] = scenario_managed_assembly_binding(base)
        cases["managed_publication_binding"] = scenario_managed_publication_binding(base)
        cases["tamper_and_drift"] = scenario_tamper_and_drift(base)
        cases["paths_and_reparse"] = scenario_paths_and_reparse(base)
        cases["verify_bridge_guards"] = scenario_verify_bridge_guards(base)
        cases["input_hardlinks"] = scenario_input_hardlinks(base)
        cases["historical_verification"] = scenario_historical_verification(base)
        cases["frozen_raw_payloads"] = scenario_frozen_raw_payloads(base)
        cases["concurrency"] = scenario_lock_concurrency(base)
        cases["publication_faults"] = scenario_publication_faults(base)
        cases["startup_pair_failures"] = scenario_startup_pair_failures(base)
        cases["startup_input_recapture"] = scenario_startup_input_recapture(base)
        cases["startup_pair_tamper"] = scenario_startup_pair_tamper(base)
        cases["canonical_typed_reconstruction"] = (
            scenario_canonical_typed_reconstruction(base)
        )
        cases["verify_late_recapture"] = scenario_verify_late_recapture(base)
        cases["descriptor_delete_guards"] = scenario_descriptor_delete_guards(base)
        cases["lock_ownership"] = scenario_lock_ownership(base)
        cases["config_claim_concurrency"] = scenario_config_claim_concurrency(base)
        cases["partial_failure"] = scenario_partial_failure(base)
        cases["autoresume_pref_gate"] = scenario_autoresume_pref_gate(base)
        cases["loader_and_confirmation"] = scenario_loader_and_confirmation(base)
        cases["runtime_bootstrap_result_contract"] = (
            scenario_runtime_bootstrap_result_contract(base)
        )
        cases["parent_finalize_lock_custody"] = (
            scenario_parent_finalize_lock_custody(base)
        )
        cases["parent_finalize_journal_append"] = (
            scenario_parent_finalize_journal_append(base)
        )
        cases["frozen_runtime_bootstrap_semantics"] = (
            scenario_frozen_runtime_bootstrap_semantics(base)
        )
    report: dict[str, Any] = {
        "schema": SELF_TEST_SCHEMA,
        "status": "passed",
        "fixture_mode": "isolated_test_root_only",
        "live_klei_writes": 0,
        "scenario_count": len(cases),
        "assertion_count": sum(len(value) for value in cases.values()),
        "production_shape": {
            "mods_entries": 22,
            "probe_entries": 1,
            "all_other_entries_preserved_but_disabled": True,
        },
        "cases": cases,
        "source_hashes": {
            "configure_live_validation_runtime.py": core.sha256_file(pathlib.Path(core.__file__)),
            "verify_live_validation_runtime_config.py": core.sha256_file(pathlib.Path(__file__)),
        },
    }
    report["report_hash"] = core.sha256_bytes(core.canonical_bytes(report))
    report_path = core.normalized(report_path)
    if not core.path_within(report_path, PROJECT_ROOT):
        raise VerificationFailure("self-test report must stay under the workspace")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary_report = report_path.parent / f".g0-report-{os.getpid()}.tmp"
    try:
        temporary_report.write_bytes(payload)
        os.replace(temporary_report, report_path)
    finally:
        temporary_report.unlink(missing_ok=True)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify", help="Read-only verification of a configured runtime.")
    core.add_common_arguments(verify_parser)
    verify_parser.add_argument("--expected-runtime-config-evidence-sha256", required=True)
    verify_parser.add_argument("--expected-startup-binding-sha256", required=True)
    self_test = subparsers.add_parser("self-test", help="Run isolated TestRoot regression fixtures.")
    self_test.add_argument("--report", default=str(REPORT))
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "self-test":
            result = run_self_test(pathlib.Path(args.report))
        else:
            args.apply = False
            args.what_if = True
            args.test_fail_after_replace = 0
            result = core.verify_evidence(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (core.ConfigError, VerificationFailure) as exc:
        print(
            json.dumps(
                {
                    "schema": core.ERROR_SCHEMA,
                    "status": "blocked",
                    "classification": "infra_fail",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        print(
            json.dumps(
                {
                    "schema": core.ERROR_SCHEMA,
                    "status": "blocked",
                    "classification": "infra_fail",
                    "error": "unexpected local I/O or data-shape failure",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
