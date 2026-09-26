#!/usr/bin/env python3
"""Offline fixture verification for the live validation environment transaction."""

from __future__ import annotations

import argparse
import copy
import hashlib
import base64
import importlib.util
import json
import os
import pathlib
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CORE = SCRIPTS / "manage_live_validation_environment.py"
CAPTURE = SCRIPTS / "capture-live-safety-boundary.ps1"
CREATE_CLONE = SCRIPTS / "create-named-test-clone.ps1"
REPORT = ROOT / "reports" / "live-validation-environment-verification.json"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

spec = importlib.util.spec_from_file_location("manage_live_validation_environment", CORE)
if spec is None or spec.loader is None:
    raise SystemExit("could not import environment transaction core")
workflow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = workflow
spec.loader.exec_module(workflow)

from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.state_quality import canonical_state_hash
from bridge import oni_bridge
from scripts import create_shutdown_evidence as shutdown_contract
from scripts import create_startup_consumption_evidence as startup_contract
from scripts import configure_live_validation_runtime as runtime_config_contract
from scripts import manage_live_runtime_bootstrap as runtime_bootstrap_contract

RUNTIME_BOOTSTRAP_LEGACY_MAX_UTF16_UNITS = 259


class VerificationFailure(RuntimeError):
    pass


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_bytes(path: pathlib.Path, value: bytes, *, stamp: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if stamp is not None:
        os.utime(path, ns=(stamp, stamp))


def make_fixture_file_writable(path: pathlib.Path) -> None:
    """Allow an adversarial TestRoot fixture to mutate a create-only evidence file."""

    os.chmod(path, 0o600)


def write_json(path: pathlib.Path, value: Any, *, stamp: int | None = None) -> None:
    write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        stamp=stamp,
    )


def write_jsonl(path: pathlib.Path, records: list[dict[str, Any]]) -> None:
    write_bytes(
        path,
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ).encode("utf-8"),
    )


def write_resealed_journal(path: pathlib.Path, entries: list[dict[str, Any]]) -> None:
    previous: str | None = None
    sealed: list[dict[str, Any]] = []
    for index, source in enumerate(entries, start=1):
        entry = copy.deepcopy(source)
        entry.pop("entry_hash", None)
        entry["schema"] = workflow.JOURNAL_SCHEMA
        entry["index"] = index
        entry["previous_entry_hash"] = previous
        entry["entry_hash"] = workflow.sha256_bytes(workflow.canonical_bytes(entry))
        previous = entry["entry_hash"]
        sealed.append(entry)
    write_bytes(path, b"".join(workflow.canonical_bytes(entry) + b"\n" for entry in sealed))


def startup_capture(payload: bytes, label: str) -> startup_contract.Capture:
    return startup_contract.Capture(
        payload,
        startup_contract.sha256_bytes(payload),
        len(payload),
        workflow.contract_canonical_hash({"fixture_source": label}),
    )


def startup_common_fields(
    binding: dict[str, Any],
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


def run(command: list[str], expected: int | set[int] = 0, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    accepted = {expected} if isinstance(expected, int) else expected
    if completed.returncode not in accepted:
        detail = (completed.stderr or completed.stdout).strip().replace("\r", " ").replace("\n", " ")
        raise VerificationFailure(
            f"command returned {completed.returncode}, expected {sorted(accepted)}: {detail[-1000:]}"
        )
    return completed


_PS_PARAMETER = re.compile(r"-[A-Za-z][A-Za-z0-9]*(?::\$(?:true|false))?")


def ps_command_text(script: pathlib.Path, arguments: tuple[str, ...]) -> str:
    """Build a -Command invocation that binds switch values on PowerShell 5.1.

    ``powershell.exe -File`` passes ``-Confirm:$false`` as the string
    "$false", which Windows PowerShell 5.1 refuses to bind to a switch.
    Parameter names (and switch values) are emitted verbatim; every other
    argument is a single-quoted literal.  No fixture value starts with "-".
    """

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    parts = ["&", quote(str(script))]
    for argument in arguments:
        parts.append(argument if _PS_PARAMETER.fullmatch(argument) else quote(argument))
    return " ".join(parts)


def ps(script: pathlib.Path, *arguments: str, expected: int | set[int] = 0) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        raise VerificationFailure("PowerShell is unavailable")
    return run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            ps_command_text(script, arguments),
        ],
        expected=expected,
    )


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def disabled_policy() -> dict[str, Any]:
    return {
        "schema": "oni.action-policy.v1",
        "actions_enabled": False,
        "suggestions_enabled": False,
        "allowed_save_hashes": [],
        "allowed_actions": ["focus_note", "noop"],
        "command_ttl_seconds": 30,
        "max_pending_commands": 20,
        "generated_at": "2026-08-30T20:00:00+00:00",
    }


def disabled_control_policy() -> dict[str, Any]:
    return {
        "schema": "oni.validation-control-policy.v1",
        "controls_enabled": False,
        "test_run_id": None,
        "allowed_operations": [],
        "allowed_save_hashes": [],
        "allowed_save_path_ids": [],
        "allowed_target_save_path_ids": [],
        "generated_at": "2026-08-30T20:00:00+00:00",
        "expires_at": None,
    }


def production_mods_document() -> dict[str, Any]:
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
            "staticID": runtime_config_contract.PROBE_STATIC_ID,
        }
    )
    return {"version": 1, "mods": mods, "mod_load_in_progress": False}


def production_mods_bytes() -> bytes:
    return json.dumps(production_mods_document(), ensure_ascii=False, indent=2).replace(
        "\n", "\r\n"
    ).encode("utf-8")


def make_bridge(root: pathlib.Path, *, stamp: int, old: bool) -> None:
    write_json(root / "action-policy.json", disabled_policy(), stamp=stamp)
    write_json(root / "validation-control-policy.json", disabled_control_policy(), stamp=stamp + 1)
    for stage in (
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
        (root / "commands" / stage).mkdir(parents=True, exist_ok=True)
    for stage in ("approved", "inflight", "done", "failed", "quarantine"):
        (root / "validation-controls" / stage).mkdir(parents=True, exist_ok=True)
    if old:
        write_bytes(root / "old-runtime-marker.txt", b"immutable-old-bridge\n", stamp=stamp + 100)


def oni_save_bytes(body: bytes) -> bytes:
    """Bytes with ONI's save header layout (SaveGame.GetHeader), non-autosave."""
    header = json.dumps({"baseName": "Fixture Base", "isAutoSave": False}).encode("utf-8")
    fields = (744825, len(header), 1, 1)  # buildVersion, headerSize, headerVersion, compression
    return b"".join(value.to_bytes(4, "little") for value in fields) + header + body


def make_fixture(
    base: pathlib.Path,
    name: str,
    run_id: str = "g0-priority-fixture",
    *,
    with_archive_root: bool = True,
    with_extra_save_pair: bool = False,
    with_old_probe: bool = True,
    establish_golden: bool = True,
) -> dict[str, pathlib.Path | str]:
    case = base / name
    oni = case / "oni-user"
    evidence = case / "evidence"
    boundaries = evidence / "live-boundaries"
    runtime = evidence / "runtime"
    save_dir = oni / "save_files" / "FixtureColony"
    source = save_dir / "Original.sav"
    source_preview = save_dir / "Original.png"
    stamp = 1_725_000_000_123_456_700
    write_bytes(source, oni_save_bytes(b"immutable-source-save-v1\x00"), stamp=stamp)
    write_bytes(source_preview, b"immutable-source-preview-v1\x89PNG", stamp=stamp + 100)
    extra_save = save_dir / "ExistingSecondary.sav"
    extra_preview = save_dir / "ExistingSecondary.png"
    if with_extra_save_pair:
        write_bytes(extra_save, b"immutable-secondary-save-v1\x00", stamp=stamp + 110)
        write_bytes(
            extra_preview,
            b"immutable-secondary-preview-v1\x89PNG",
            stamp=stamp + 120,
        )
    write_bytes(
        oni / "kplayerprefs.yaml",
        (
            "strings:\r\n"
            "  InstalledLanguage: zh_klei\r\n"
            f"  SaveFilenameKey/: {source}\r\n"
            "ints:\r\n"
            "  SESSION_ID: 1\r\n"
            "floats:\r\n"
            "  Volume_Master: 1\r\n"
        ).encode("utf-8"),
        stamp=stamp + 200,
    )
    write_bytes(oni / "mods" / "mods.json", production_mods_bytes(), stamp=stamp + 300)
    write_bytes(oni / "mods" / "mods.json.bak", production_mods_bytes(), stamp=stamp + 400)
    (oni / "mods" / "Dev").mkdir(parents=True, exist_ok=True)
    if with_old_probe:
        write_bytes(
            oni / "mods" / "Dev" / "oni-ai-colony-agent" / "OniAiProbeMod.dll",
            b"baseline-probe-dll",
            stamp=stamp + 500,
        )
        write_bytes(
            oni / "mods" / "Dev" / "oni-ai-colony-agent" / "nested" / "contract.json",
            b'{"baseline":true}\n',
            stamp=stamp + 600,
        )
    preexisting_archive_file = oni / "ai-bridge-archives" / "historical-fixture" / "nested" / "evidence.json"
    if with_archive_root:
        write_bytes(preexisting_archive_file, b'{"historical":true}\n', stamp=stamp + 650)
    make_bridge(oni / "ai-bridge", stamp=stamp + 700, old=True)
    boundaries.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    release_dll = case / "workspace" / "mod" / "OniAiProbeMod.dll"
    write_bytes(release_dll, b"production-shaped-release-probe-0.7.0\x00", stamp=stamp + 675)
    write_bytes(
        release_dll.with_name("OniAiProbeMod.pdb"),
        b"production-shaped-release-probe-symbols-0.7.0\x00",
        stamp=stamp + 676,
    )
    write_bytes(
        release_dll.with_name("mod.yaml"),
        b'title: "ONI AI Probe"\r\nstaticID: "ass.OniAiProbe"\r\n',
        stamp=stamp + 677,
    )
    write_bytes(
        release_dll.with_name("mod_info.yaml"),
        b"APIVersion: 2\r\n",
        stamp=stamp + 678,
    )
    game_managed_dir = case / "game" / "OxygenNotIncluded_Data" / "Managed"
    managed_csharp = game_managed_dir / "Assembly-CSharp.dll"
    managed_firstpass = game_managed_dir / "Assembly-CSharp-firstpass.dll"
    write_bytes(managed_csharp, b"fixture-managed-assembly-csharp\x00", stamp=stamp + 676)
    write_bytes(managed_firstpass, b"fixture-managed-assembly-firstpass\x00", stamp=stamp + 677)
    idle = case / "process-idle.json"
    game = case / "process-game.json"
    write_json(
        idle,
        {
            "schema": "oni.process-state-fixture.v1",
            "oxygen_not_included_count": 0,
            "bridge_count": 0,
        },
    )
    write_json(
        game,
        {
            "schema": "oni.process-state-fixture.v1",
            "oxygen_not_included_count": 1,
            "bridge_count": 0,
        },
    )
    bootstrap_idle = case / "process-bootstrap-idle.json"
    write_json(
        bootstrap_idle,
        {
            "OxygenNotIncluded": 0,
            "oni_bridge.py": 0,
        },
    )
    fixture: dict[str, pathlib.Path | str] = {
        "case": case,
        "oni": oni,
        "evidence": evidence,
        "boundaries": boundaries,
        "runtime": runtime,
        "save_dir": save_dir,
        "source": source,
        "source_preview": source_preview,
        "extra_save": extra_save,
        "extra_preview": extra_preview,
        "idle": idle,
        "game": game,
        "bootstrap_idle": bootstrap_idle,
        "preexisting_archive_file": preexisting_archive_file,
        "release_dll": release_dll,
        "expected_release_dll_sha256": sha256(release_dll),
        "game_managed_dir": game_managed_dir,
        "managed_csharp_sha256": sha256(managed_csharp),
        "managed_firstpass_sha256": sha256(managed_firstpass),
        "old_probe_present": with_old_probe,
        "run_id": run_id,
        "loader_stem": f"{run_id}-loader",
    }
    capture_pre(fixture)
    if establish_golden:
        # The fixture's originals are verified by construction; record them as
        # the cross-run golden baseline every prepare checks (review S2).
        pre_path = fixture_paths(fixture)["pre"]
        workflow.establish_golden_baseline(
            evidence, json.loads(pre_path.read_text(encoding="utf-8-sig")), pre_path
        )
    create_loader_clone(fixture)
    return fixture


def capture_pre(fixture: dict[str, pathlib.Path | str]) -> None:
    ps(
        CAPTURE,
        "-RunId",
        str(fixture["run_id"]),
        "-Phase",
        "pre",
        "-OniUserRoot",
        str(fixture["oni"]),
        "-ReportRoot",
        str(fixture["boundaries"]),
        "-TestRoot",
        str(fixture["case"]),
        "-ProcessStateFixture",
        str(fixture["idle"]),
    )


def create_loader_clone(fixture: dict[str, pathlib.Path | str]) -> None:
    ps(
        CREATE_CLONE,
        "-SourceSave",
        str(fixture["source"]),
        "-TargetStem",
        str(fixture["loader_stem"]),
        "-RunId",
        str(fixture["run_id"]),
        "-OniUserRoot",
        str(fixture["oni"]),
        "-ReportRoot",
        str(fixture["runtime"]),
        "-TestRoot",
        str(fixture["case"]),
        "-ProcessStateFixture",
        str(fixture["idle"]),
        "-Confirm:$false",
    )


def fixture_paths(fixture: dict[str, pathlib.Path | str]) -> dict[str, pathlib.Path]:
    run_id = str(fixture["run_id"])
    loader_stem = str(fixture["loader_stem"])
    # The loader clone has its own colony folder beside the source colony.
    loader_dir = pathlib.Path(fixture["save_dir"]).parent / loader_stem
    evidence = pathlib.Path(fixture["evidence"])
    return {
        "pre": pathlib.Path(fixture["boundaries"]) / run_id / "pre-boundary.json",
        "clone": pathlib.Path(fixture["runtime"]) / run_id / "test-clone-manifest.json",
        "loader": loader_dir / f"{loader_stem}.sav",
        "loader_preview": loader_dir / f"{loader_stem}.png",
        "environment": evidence / run_id / "environment",
        "manifest": evidence / run_id / "environment" / "environment-manifest.json",
    }


def common_args(fixture: dict[str, pathlib.Path | str]) -> list[str]:
    paths = fixture_paths(fixture)
    return [
        "--run-id",
        str(fixture["run_id"]),
        "--oni-root",
        str(fixture["oni"]),
        "--evidence-root",
        str(fixture["evidence"]),
        "--boundary-report-root",
        str(fixture["boundaries"]),
        "--pre-boundary",
        str(paths["pre"]),
        "--clone-manifest",
        str(paths["clone"]),
        "--source-save",
        str(fixture["source"]),
        "--loader-save",
        str(paths["loader"]),
        "--loader-stem",
        str(fixture["loader_stem"]),
        "--test-root",
        str(fixture["case"]),
        "--process-state-fixture",
        str(fixture["idle"]),
    ]


def finalize_binding_args(fixture: dict[str, pathlib.Path | str]) -> list[str]:
    paths = fixture_paths(fixture)
    bridge = pathlib.Path(fixture["oni"]) / "ai-bridge"
    return [
        "--manifest",
        str(paths["manifest"]),
        "--lineage-manifest",
        str(bridge / "evidence" / "save-lineage" / str(fixture["run_id"]) / "manifest.json"),
        "--priority-pair-report",
        str(bridge / "evidence" / "save-lineage" / str(fixture["run_id"]) / "priority-pair-finalization.json"),
        "--priority-pair-report-hash",
        str(fixture["pair_report_hash"]),
        "--shutdown-evidence",
        str(fixture["shutdown_evidence_path"]),
        "--shutdown-evidence-hash",
        str(fixture["shutdown_evidence_hash"]),
        "--startup-consumption-bundle",
        str(fixture["startup_consumption_bundle"]),
        "--startup-consumption-evidence-hash",
        str(fixture["startup_consumption_evidence_hash"]),
        "--validation-process-id",
        str(fixture["validation_process_id"]),
        "--runtime-config-evidence",
        str(fixture["runtime_config_evidence_path"]),
        "--expected-runtime-config-evidence-sha256",
        str(fixture["runtime_config_evidence_sha256"]),
        "--expected-startup-binding-sha256",
        str(fixture["startup_binding_sha256"]),
        "--runtime-bootstrap-result",
        str(fixture["runtime_bootstrap_result"]),
        "--expected-runtime-bootstrap-result-sha256",
        str(fixture["expected_runtime_bootstrap_result_sha256"]),
        "--release-dll",
        str(fixture["release_dll"]),
        "--expected-release-dll-sha256",
        str(fixture["expected_release_dll_sha256"]),
        "--game-managed-dir",
        str(fixture["game_managed_dir"]),
        "--test-managed-assembly-csharp-sha256",
        str(fixture["managed_csharp_sha256"]),
        "--test-managed-assembly-csharp-firstpass-sha256",
        str(fixture["managed_firstpass_sha256"]),
        "--forward-step-id",
        "priority_forward",
        "--inverse-step-id",
        "priority_inverse",
        "--command-id",
        "cmd-priority-forward-fixture",
        "--command-id",
        "cmd-priority-inverse-fixture",
        "--command-bundle-hash",
        str(fixture["forward_bundle_hash"]),
        "--command-bundle-hash",
        str(fixture["inverse_bundle_hash"]),
    ]


def abort_binding_args(fixture: dict[str, pathlib.Path | str]) -> list[str]:
    paths = fixture_paths(fixture)
    return [
        "--manifest",
        str(paths["manifest"]),
        "--expected-environment-manifest-sha256",
        sha256(paths["manifest"]),
        "--runtime-config-evidence",
        str(fixture["runtime_config_evidence_path"]),
        "--expected-runtime-config-evidence-sha256",
        str(fixture["runtime_config_evidence_sha256"]),
        "--expected-startup-binding-sha256",
        str(fixture["startup_binding_sha256"]),
        "--runtime-bootstrap-result",
        str(fixture["runtime_bootstrap_result"]),
        "--expected-runtime-bootstrap-result-sha256",
        str(fixture["expected_runtime_bootstrap_result_sha256"]),
        "--release-dll",
        str(fixture["release_dll"]),
        "--expected-release-dll-sha256",
        str(fixture["expected_release_dll_sha256"]),
        "--game-managed-dir",
        str(fixture["game_managed_dir"]),
        "--test-managed-assembly-csharp-sha256",
        str(fixture["managed_csharp_sha256"]),
        "--test-managed-assembly-csharp-firstpass-sha256",
        str(fixture["managed_firstpass_sha256"]),
        "--confirm-live-run-id",
        str(fixture["run_id"]),
    ]


def core(
    fixture: dict[str, pathlib.Path | str],
    command: str,
    *extra: str,
    apply: bool = False,
    what_if: bool = False,
    expected: int | set[int] = 0,
) -> subprocess.CompletedProcess[str]:
    arguments = [sys.executable, str(CORE), command, *common_args(fixture)]
    if command == "finalize":
        arguments.extend(finalize_binding_args(fixture))
    elif command == "abort":
        arguments.extend(abort_binding_args(fixture))
    arguments.extend(extra)
    if apply:
        arguments.append("--apply")
    elif what_if:
        arguments.append("--what-if")
    return run(arguments, expected=expected, timeout=120)


def prepare_fixture(fixture: dict[str, pathlib.Path | str]) -> None:
    result = core(fixture, "prepare", apply=True)
    payload = json.loads(result.stdout)
    assert_true(payload.get("status") == "prepared", "prepare did not publish a manifest")


def make_command_bundle(
    bridge: pathlib.Path,
    command_id: str,
    *,
    sequence: int,
) -> tuple[
    dict[str, Any],
    pathlib.Path,
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
    pathlib.Path,
]:
    action = "set_duplicant_chore_priority"
    save_id_hash = "sha256:" + "8" * 64
    state_hash = "sha256:" + hashlib.sha256(f"state-{sequence}".encode("utf-8")).hexdigest()
    source_policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "suggestions_enabled": False,
        "allowed_actions": [action],
        "allowed_save_hashes": [save_id_hash],
    }
    source_policy_hash = workflow.contract_canonical_hash(source_policy)
    frame: dict[str, Any] = {
        "schema": "oni.decision-frame.v2",
        "frame_id": "",
        "source": {
            "session_id": f"fixture-session-{sequence}",
            "save_id_hash": save_id_hash,
            "world_id": "fixture-world",
            "snapshot_id": f"snapshot-{sequence}",
            "state_hash": state_hash,
            "seq": sequence,
            "snapshot_seq": sequence,
            "ts": f"2026-08-30T21:00:{sequence:02d}+00:00",
        },
        "trust": {"status": "trusted"},
        "readiness": {},
        "blockers": [],
        "projections": {"policy": {"policy_hash": source_policy_hash}},
    }
    frame["frame_id"] = workflow.sha256_bytes(
        workflow.canonical_bytes({key: value for key, value in frame.items() if key != "frame_id"})
    )
    command = {
        "schema": "oni.command.v3",
        "id": command_id,
        "status": "executed_pending_snapshot",
        "action": action,
        "args": {"duplicant_instance_id": 17, "chore_group": "Dig", "priority": sequence},
        "session_id": f"fixture-session-{sequence}",
        "save_id_hash": save_id_hash,
        "world_id": "fixture-world",
        "source_snapshot_id": f"snapshot-{sequence}",
        "state_hash": state_hash,
        "source_frame_id": frame["frame_id"],
        "source": {
            "policy_hash": source_policy_hash,
            "state_seq": sequence,
            "state_ts": frame["source"]["ts"],
        },
        "attempt_id": f"attempt-{sequence:032d}",
        "accepted_ack_seq": sequence + 1,
        "executed_ack_seq": sequence + 2,
        "local_effect_ack_seq": sequence + 3,
        "completed_at": f"2026-08-30T21:01:{sequence:02d}+00:00",
    }
    before_state = {"fixture_priority": sequence - 1}
    after_state = {"fixture_priority": sequence}
    witness = {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "session_id": command["session_id"],
        "seq": sequence + 4,
        "snapshot_seq": sequence + 4,
        "snapshot_id": f"witness-{sequence}",
        "ts": f"2026-08-30T21:02:{sequence:02d}+00:00",
        "save_id_hash": save_id_hash,
        "world_id": command["world_id"],
        "save_dirty": False,
    }
    witness_hash = canonical_state_hash(witness)
    command["result"] = {
        "before_state": before_state,
        "after_state": after_state,
    }
    intent_hash = workflow.command_intent_hash(command)
    receipt: dict[str, Any] = {
        "schema": "oni.approval-receipt.v1",
        "command_id": command_id,
        "command_intent_hash": intent_hash,
        "source_frame_id": frame["frame_id"],
        "source": {
            "session_id": command["session_id"],
            "save_id_hash": save_id_hash,
            "world_id": command["world_id"],
            "snapshot_id": command["source_snapshot_id"],
            "state_hash": state_hash,
        },
        "policy_hash": source_policy_hash,
        "approved_by": "fixture",
        "approved_at": "2026-08-30T21:01:00+00:00",
        "receipt_hash": "",
    }
    receipt["receipt_hash"] = workflow.contract_hash_without(receipt, "receipt_hash", "fixture receipt")
    command["approval_receipt"] = receipt
    outcome: dict[str, Any] = {
        "schema": "oni.action-outcome.v1",
        "command_id": command_id,
        "action": action,
        "command_intent_hash": workflow.command_intent_hash(command),
        "command_location": "confirming",
        "executor_status": "executed_pending_snapshot",
        "confirmation_status": "snapshot_confirmed",
        "classification": None,
        "blockers": [],
        "source": {
            "snapshot_id": command["source_snapshot_id"],
            "seq": sequence,
            "state_hash": command["state_hash"],
        },
        "direct": {
            "completed_at": command["completed_at"],
            "before_state_hash": canonical_state_hash(before_state),
            "after_state_hash": canonical_state_hash(after_state),
        },
        "witness": {
            "snapshot_id": witness["snapshot_id"],
            "seq": witness["seq"],
            "ts": witness["ts"],
            "state_hash": witness_hash,
        },
        "attestation_hash": "",
    }
    outcome["attestation_hash"] = workflow.contract_hash_without(
        outcome, "attestation_hash", "fixture action outcome"
    )
    command.update(
        {
            "status": "confirmed",
            "confirmed_at": witness["ts"],
            "confirmation": outcome,
        }
    )
    command["result"].update(
        {
            "classification": "snapshot_confirmed_mutation_v1",
            "confirmed_snapshot_id": witness["snapshot_id"],
            "confirmed_seq": witness["seq"],
            "confirmed_state_hash": witness_hash,
            "attestation_hash": outcome["attestation_hash"],
        }
    )
    records = [
        {
            "schema": "oni.event.v2",
            "event": "command_ack",
            "session_id": command["session_id"],
            "id": command_id,
            "attempt_id": command["attempt_id"],
            "status": status,
            "seq": command[field],
        }
        for status, field in (
            ("accepted", "accepted_ack_seq"),
            ("executed", "executed_ack_seq"),
            ("local_effect_observed", "local_effect_ack_seq"),
        )
    ]
    records.append(witness)
    frame_digest = frame["frame_id"].removeprefix("sha256:")
    frame_path = bridge / "decision-frames" / f"sha256-{frame_digest}.json"
    write_json(frame_path, frame)
    frame_file_sha256 = sha256(frame_path)
    bundle: dict[str, Any] = {
        "schema": "oni.confirmed-command-approval-bundle.v1",
        "command_id": command_id,
        "action": action,
        "command_intent_hash": intent_hash,
        "final_command_hash": workflow.contract_canonical_hash(command),
        "final_command": command,
        "source_policy_hash": source_policy_hash,
        "source_policy": {
            "policy_hash": source_policy_hash,
            "file_sha256": workflow.sha256_bytes(workflow.canonical_bytes(source_policy)),
            "document": source_policy,
        },
        "approval_receipt_hash": receipt["receipt_hash"],
        "source_frame": {
            "frame_id": frame["frame_id"],
            "archive_file_sha256": frame_file_sha256,
            "document": frame,
        },
        "bundle_hash": "",
    }
    bundle["bundle_hash"] = workflow.contract_hash_without(bundle, "bundle_hash", "fixture bundle")
    digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    bundle_path = bridge / "confirmed-command-evidence" / digest / "approval-bundle.json"
    write_json(bundle_path, bundle)
    write_json(bridge / "commands" / "done" / f"{command_id}.json", command)
    certificate: dict[str, Any] = {
        "schema": "oni.action-outcomes.v1",
        "updated_at": "2026-08-30T21:03:00+00:00",
        "source": {
            "session_id": command["session_id"],
            "save_id_hash": command["save_id_hash"],
            "world_id": command["world_id"],
            "snapshot_id": outcome["witness"]["snapshot_id"],
            "seq": outcome["witness"]["seq"],
            "state_hash": outcome["witness"]["state_hash"],
        },
        "trust": {"status": "trusted", "blockers": [], "replay_mode": False},
        "counts": {"snapshot_confirmed": 1},
        "outcomes": [outcome],
        "outcomes_hash": "",
    }
    certificate["outcomes_hash"] = workflow.contract_hash_without(
        certificate, "outcomes_hash", "fixture confirmation certificate"
    )
    certificate_path = bridge / "sessions" / command["session_id"] / "action-outcomes.json"
    write_json(certificate_path, certificate)
    return bundle, bundle_path, outcome, records, certificate, certificate_path


def configure_runtime_fixture(fixture: dict[str, pathlib.Path | str]) -> dict[str, Any]:
    paths = fixture_paths(fixture)
    arguments = runtime_config_contract.build_parser().parse_args(
        [
            "--run-id",
            str(fixture["run_id"]),
            "--oni-root",
            str(fixture["oni"]),
            "--evidence-root",
            str(fixture["evidence"]),
            "--boundary-report-root",
            str(fixture["boundaries"]),
            "--environment-manifest",
            str(paths["manifest"]),
            "--expected-environment-manifest-sha256",
            sha256(paths["manifest"]),
            "--runtime-bootstrap-result",
            str(fixture["runtime_bootstrap_result"]),
            "--expected-runtime-bootstrap-result-sha256",
            str(fixture["expected_runtime_bootstrap_result_sha256"]),
            "--pre-boundary",
            str(paths["pre"]),
            "--clone-manifest",
            str(paths["clone"]),
            "--source-save",
            str(fixture["source"]),
            "--loader-save",
            str(paths["loader"]),
            "--loader-stem",
            str(fixture["loader_stem"]),
            "--release-dll",
            str(fixture["release_dll"]),
            "--expected-release-dll-sha256",
            str(fixture["expected_release_dll_sha256"]),
            "--game-managed-dir",
            str(fixture["game_managed_dir"]),
            "--test-managed-assembly-csharp-sha256",
            str(fixture["managed_csharp_sha256"]),
            "--test-managed-assembly-csharp-firstpass-sha256",
            str(fixture["managed_firstpass_sha256"]),
            "--test-root",
            str(fixture["case"]),
            "--process-state-fixture",
            str(fixture["idle"]),
            "--apply",
            "--confirm-live-run-id",
            str(fixture["run_id"]),
        ]
    )
    result = runtime_config_contract.configure(arguments)
    evidence_path = paths["environment"] / workflow.RUNTIME_CONFIG_EVIDENCE_NAME
    evidence_hash = runtime_config_contract.sha256_file(evidence_path)
    startup_binding_path = (
        pathlib.Path(fixture["oni"])
        / "ai-bridge"
        / runtime_config_contract.STARTUP_BINDING_NAME
    )
    startup_binding_hash = runtime_config_contract.sha256_file(startup_binding_path)
    if result.get("runtime_config_evidence_sha256") != evidence_hash:
        raise VerificationFailure("runtime config apply did not return its exact evidence SHA-256")
    if result.get("startup_binding_sha256") != startup_binding_hash:
        raise VerificationFailure("runtime config apply did not return its exact startup binding SHA-256")
    arguments.apply = False
    arguments.what_if = True
    arguments.test_fail_after_replace = 0
    arguments.expected_runtime_config_evidence_sha256 = evidence_hash
    arguments.expected_startup_binding_sha256 = startup_binding_hash
    verified = runtime_config_contract.verify_evidence(arguments)
    if verified.get("status") != "passed":
        raise VerificationFailure("runtime config independent historical verification did not pass")
    fixture["runtime_config_evidence_path"] = evidence_path
    fixture["runtime_config_evidence_sha256"] = evidence_hash
    fixture["startup_binding_path"] = startup_binding_path
    fixture["startup_binding_sha256"] = startup_binding_hash
    fixture["runtime_config_verification_report_hash"] = verified["report_hash"]
    return verified


def install_runtime_bootstrap_fixture(
    fixture: dict[str, pathlib.Path | str],
) -> dict[str, Any]:
    """Install the four-file Probe and publish the authentic exact38 ready result."""

    paths = fixture_paths(fixture)
    result_path = (
        pathlib.Path(fixture["evidence"])
        / str(fixture["run_id"])
        / "runtime-bootstrap"
        / "result.json"
    )
    common = [
        "--operation",
        "install",
        "--run-id",
        str(fixture["run_id"]),
        "--oni-root",
        str(fixture["oni"]),
        "--evidence-root",
        str(fixture["evidence"]),
        "--environment-manifest",
        str(paths["manifest"]),
        "--expected-environment-manifest-sha256",
        sha256(paths["manifest"]),
        "--release-dll",
        str(fixture["release_dll"]),
        "--expected-release-dll-sha256",
        str(fixture["expected_release_dll_sha256"]),
        "--expected-mod-version",
        runtime_bootstrap_contract.EXPECTED_MOD_VERSION,
        "--confirm-live-run-id",
        str(fixture["run_id"]),
        "--test-root",
        str(fixture["case"]),
        "--process-state-fixture",
        str(fixture["bootstrap_idle"]),
    ]
    stage_nonce = workflow.sha256_bytes(
        f"{fixture['run_id']}:environment-fixture-bootstrap-stage".encode("utf-8")
    ).removeprefix("sha256:")[:32]
    before_what_if = file_inventory(pathlib.Path(fixture["case"]))
    forecast_args = runtime_bootstrap_contract.build_parser().parse_args(
        [*common, "--what-if", "--stage-nonce", stage_nonce]
    )
    forecast = runtime_bootstrap_contract.execute(forecast_args)
    if (
        forecast.get("status") != "would_install"
        or forecast.get("write_count") != 0
        or forecast.get("game_launched") is not False
        or result_path.exists()
        or file_inventory(pathlib.Path(fixture["case"])) != before_what_if
    ):
        raise VerificationFailure("runtime bootstrap WhatIf was not exactly read-only")
    expected_stage_sha256 = forecast.get("expected_stage_sha256")
    expected_transaction_id = forecast.get("transaction_id")
    if (
        not isinstance(expected_stage_sha256, str)
        or not isinstance(expected_transaction_id, str)
    ):
        raise VerificationFailure("runtime bootstrap WhatIf omitted its external stage anchors")
    staged_args = runtime_bootstrap_contract.build_parser().parse_args(
        [
            *common,
            "--stage",
            "--stage-nonce",
            stage_nonce,
            "--expected-stage-sha256",
            expected_stage_sha256,
            "--expected-transaction-id",
            expected_transaction_id,
        ]
    )
    staged = runtime_bootstrap_contract.execute(staged_args)
    if (
        staged.get("status") != "staged"
        or staged.get("stage_sha256") != expected_stage_sha256
        or staged.get("transaction_id") != expected_transaction_id
    ):
        raise VerificationFailure("runtime bootstrap stage did not preserve WhatIf anchors")
    apply_args = runtime_bootstrap_contract.build_parser().parse_args(
        [
            *common,
            "--apply",
            "--stage-nonce",
            stage_nonce,
            "--expected-stage-sha256",
            expected_stage_sha256,
            "--expected-transaction-id",
            expected_transaction_id,
        ]
    )
    result = runtime_bootstrap_contract.execute(apply_args)
    expected_old_probe_recovery = (
        "recovery/old-probe" if fixture.get("old_probe_present") is True else None
    )
    if (
        result.get("status") != "installed_ready"
        or result.get("game_launched") is not False
        or len(result) != 38
        or set(result) != runtime_config_contract.RUNTIME_BOOTSTRAP_RESULT_KEYS
        or result.get("old_probe_recovery_relative")
        != expected_old_probe_recovery
        or not result_path.is_file()
    ):
        raise VerificationFailure(
            "runtime bootstrap did not publish the exact installed_ready 38-key result"
        )
    installed = pathlib.Path(fixture["oni"]) / "mods" / "Dev" / "oni-ai-colony-agent"
    installed_relatives = {
        path.relative_to(installed).as_posix() for path in installed.rglob("*")
    }
    if installed_relatives != set(
        runtime_config_contract.RUNTIME_BOOTSTRAP_ARTIFACT_NAMES
    ):
        raise VerificationFailure("runtime bootstrap fixture is not the exact four-file Probe")
    recovery_root = result_path.parent / "recovery"
    old_probe_recovery = recovery_root / "old-probe"
    if fixture.get("old_probe_present") is True:
        if not old_probe_recovery.is_dir():
            raise VerificationFailure("runtime bootstrap omitted the old-Probe recovery tree")
    elif old_probe_recovery.exists() or (
        recovery_root.exists() and any(recovery_root.iterdir())
    ):
        raise VerificationFailure(
            "runtime bootstrap created a recovery tree for an originally absent Probe"
        )
    result_hash = sha256(result_path)
    fixture["runtime_bootstrap_result"] = result_path
    fixture["expected_runtime_bootstrap_result_sha256"] = result_hash
    fixture["runtime_bootstrap_what_if_stage_sha256"] = expected_stage_sha256
    fixture["runtime_bootstrap_transaction_id"] = expected_transaction_id
    return result


def runtime_bootstrap_install_path_budget(
    fixture: Mapping[str, pathlib.Path | str],
) -> dict[str, int]:
    roots = runtime_bootstrap_contract.Roots(
        run_id=str(fixture["run_id"]),
        oni_root=pathlib.Path(fixture["oni"]),
        evidence_root=pathlib.Path(fixture["evidence"]),
        test_root=pathlib.Path(fixture["case"]),
    )
    plan_path = roots.transaction_dir / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    candidates = runtime_bootstrap_contract.legacy_path_candidates(roots, plan)
    return {
        "max_utf16_units": max(
            runtime_bootstrap_contract.utf16_units(path) for path in candidates
        ),
        "enumerated_path_count": len(candidates),
    }


def is_persistent_runtime_bootstrap_environment_lock_claim(
    path: pathlib.Path,
    fixture: Mapping[str, pathlib.Path | str],
) -> bool:
    expected_parent = (
        pathlib.Path(fixture["evidence"])
        / str(fixture["run_id"])
        / "runtime-bootstrap"
        / "environment-lock-attempts"
    )
    return (
        path.parent == expected_parent
        and expected_parent.is_dir()
        and not workflow.is_reparse(expected_parent)
        and re.fullmatch(
            r"[0-9]{4}-(?:install|replay|abort)\.claim\.json",
            path.name,
        )
        is not None
        and path.is_file()
        and not workflow.is_reparse(path)
    )


def prepare_configured_runtime(
    fixture: dict[str, pathlib.Path | str],
    *,
    add_diagnostics: bool = False,
) -> dict[str, pathlib.Path]:
    """Prepare/install/configure a runtime without publishing any session/action evidence."""

    oni = pathlib.Path(fixture["oni"])
    run_id = str(fixture["run_id"])
    install_runtime_bootstrap_fixture(fixture)
    bridge = oni / "ai-bridge"
    probe = oni / "mods" / "Dev" / "oni-ai-colony-agent"
    bootstrap = json.loads(
        (bridge / "v27-runtime-bootstrap.json").read_text(encoding="utf-8")
    )
    archived_old_raw = bootstrap.get("archived_previous_bridge")
    if not isinstance(archived_old_raw, str) or not archived_old_raw:
        raise VerificationFailure("runtime bootstrap fixture did not archive the old Bridge")
    archived_old = pathlib.Path(archived_old_raw)
    configure_runtime_fixture(fixture)
    if add_diagnostics:
        write_json(
            bridge / "health.json",
            {"schema": "oni.bridge-health.v1", "status": "waiting", "session_id": None},
        )
        write_json(
            bridge / "sessions" / "waiting" / "loader-failure.json",
            {
                "schema": "oni.loader-failure-diagnostic.v1",
                "status": "load_failed_before_session",
                "session_id": None,
            },
        )
        write_jsonl(
            bridge / "state.jsonl",
            [
                {
                    "schema": "oni.process-diagnostic.v1",
                    "event": "process_waiting",
                    "session_id": None,
                }
            ],
        )
    return {"bridge": bridge, "archived_old": archived_old, "probe": probe}


def simulate_runtime(
    fixture: dict[str, pathlib.Path | str],
    *,
    evict_forward_frame: bool = False,
    evict_forward_certificate: bool = False,
) -> dict[str, Any]:
    oni = pathlib.Path(fixture["oni"])
    run_id = str(fixture["run_id"])
    paths = fixture_paths(fixture)
    prepared = prepare_configured_runtime(fixture)
    bridge = prepared["bridge"]
    archived_old = prepared["archived_old"]
    probe = prepared["probe"]
    runtime_config_path = pathlib.Path(fixture["runtime_config_evidence_path"])
    startup_binding_path = pathlib.Path(fixture["startup_binding_path"])
    runtime_config_payload = runtime_config_path.read_bytes()
    startup_binding_payload = startup_binding_path.read_bytes()
    runtime_config_document = json.loads(runtime_config_payload.decode("utf-8"))
    startup_binding_document = json.loads(startup_binding_payload.decode("utf-8"))
    startup_binding_hash = sha256(startup_binding_path)
    validation_process_id = 4242
    # Model legitimate runtime policy churn: the historical config evidence
    # retains the initial disabled document while the shutdown pipeline binds a
    # later, still strict disabled/unbound policy document.
    runtime_policy = disabled_policy()
    runtime_policy["generated_at"] = "2026-08-31T00:00:01+00:00"
    write_json(bridge / "action-policy.json", runtime_policy)
    write_bytes(oni / "kplayerprefs.yaml", b"SaveFilenameKey: RuntimeDerivative\n")
    write_bytes(oni / "mods" / "mods.json", b'{"version":1,"mods":["runtime"]}\n')
    write_bytes(oni / "mods" / "mods.json.bak", b'{"runtime":true}\n')
    write_bytes(oni / "mods" / "config.json", b'{"runtime_only":true}\n')
    write_bytes(probe / "runtime" / "new-file.bin", b"runtime-only")
    write_bytes(paths["loader"], b"mutated-loader-save")
    write_bytes(paths["loader_preview"], b"mutated-loader-preview")

    # ONI saves the lineage checkpoint beside the active (loader) save.
    derivative = paths["loader"].parent / f"{run_id}-lineage.sav"
    derivative_preview = derivative.with_suffix(".png")
    write_bytes(derivative, f"lineage-final-save:{run_id}".encode("utf-8"))
    write_bytes(derivative_preview, b"lineage-final-preview")

    configured_at = datetime.fromisoformat(
        str(runtime_config_document["configured_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    # Equality is intentionally valid: configuration may complete at the exact
    # instant the trusted S0 origin session starts.  The remaining sessions are
    # compressed into ten milliseconds so Bridge publication cannot precede a
    # synthetic future tip on fast hosts.
    session_started_time = configured_at

    def timestamp(offset_seconds: float) -> str:
        return (session_started_time + timedelta(seconds=offset_seconds * 0.01)).isoformat(
            timespec="microseconds"
        )

    session_ids = {
        "origin": f"session-{run_id}-origin",
        "activation": f"session-{run_id}-activation",
        "forward": f"session-{run_id}-post-forward",
        "final": f"session-{run_id}-post-inverse-final",
    }
    save_hashes = {
        "origin": startup_binding_document["requested_loader_save_sha256"],
        "activation": workflow.sha256_bytes(f"save:{run_id}:activation".encode("utf-8")),
        "forward": workflow.sha256_bytes(f"save:{run_id}:forward".encode("utf-8")),
        "final": sha256(derivative),
    }
    assert_true(
        len(set(session_ids.values())) == 4 and len(set(save_hashes.values())) == 4,
        "fixture does not rotate all four session/save identities",
    )
    final_world_id = 0
    derivative_path_id = workflow.path_id(derivative, contract="upper")

    def common(
        stage: str,
        *,
        save_id_hash: str | None = None,
        active_save_path_id: str | None = None,
    ) -> dict[str, Any]:
        fields = startup_common_fields(
            startup_binding_document,
            startup_binding_hash,
            validation_process_id,
            session_ids[stage],
            world_id=final_world_id,
            save_id_hash=save_id_hash or save_hashes[stage],
        )
        if active_save_path_id is not None:
            fields["active_save_path_id"] = active_save_path_id
        elif stage != "origin":
            fields["active_save_path_id"] = derivative_path_id
        return fields

    def state(
        stage: str,
        seq: int,
        offset: float,
        snapshot_seq: int,
        *,
        save_id_hash: str | None = None,
        active_save_path_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot_ts = timestamp(offset)
        return {
            **common(
                stage,
                save_id_hash=save_id_hash,
                active_save_path_id=active_save_path_id,
            ),
            "schema": "oni.state.v2",
            "event": "state_snapshot",
            "seq": seq,
            "snapshot_seq": snapshot_seq,
            "snapshot_id": f"{session_ids[stage]}:{seq}",
            "ts": snapshot_ts,
            "observation_scope": {
                "active_world_id": final_world_id,
                "populated_world_ids": [final_world_id],
                "covered_world_ids": [final_world_id],
                "complete_for_populated_worlds": True,
            },
            "cycle": 10,
            "game": {"paused": True},
            "duplicants": {"total": 3, "stressed": 0, "idle": 0},
            "resources": {
                "food_kcal": 100_000.0,
                "oxygen_kg_estimate": 100.0,
                "water_kg_estimate": 1_000.0,
            },
            "alerts": [],
            "research": {"current": "FarmingTech", "complete": []},
            "buildings": {
                "oxygen_diffuser": 1,
                "outhouse": 1,
                "manual_generator": 1,
                "captured_at": snapshot_ts,
                "_scanned": 3,
                "_source": "test_fixture",
                "_sample_ids": ["OxygenDiffuser", "Outhouse", "ManualGenerator"],
                "_top_prefabs": [],
            },
            "quality": {
                "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
                "cycle": {"status": "observed", "source": "fixture"},
                "duplicants.total": contract_quality_fixture(DUPLICANTS_TOTAL_CONTRACT_ID),
                "duplicants.stressed": contract_quality_fixture(DUPLICANTS_STRESSED_CONTRACT_ID),
                "duplicants.idle": contract_quality_fixture(DUPLICANTS_IDLE_CONTRACT_ID),
                "resources.food_kcal": contract_quality_fixture(REGISTERED_FOOD_CONTRACT_ID),
                "resources.oxygen_kg_estimate": {"status": "observed", "source": "fixture"},
                "resources.water_kg_estimate": {"status": "observed", "source": "fixture"},
                "alerts": contract_quality_fixture(ALERTS_CANONICAL_CONTRACT_ID),
                "research.current": contract_quality_fixture(RESEARCH_CURRENT_CONTRACT_ID),
                "research.complete": {"status": "observed", "source": "fixture"},
                "buildings.oxygen_diffuser": contract_quality_fixture(
                    BUILDING_COMPLETED_COUNT_CONTRACT_ID
                ),
                "buildings.outhouse": contract_quality_fixture(
                    BUILDING_COMPLETED_COUNT_CONTRACT_ID
                ),
                "buildings.manual_generator": contract_quality_fixture(
                    BUILDING_COMPLETED_COUNT_CONTRACT_ID
                ),
                "buildings._source": {"status": "observed", "source": "fixture"},
                "buildings._scanned": {"status": "observed", "source": "fixture"},
            },
            "sample": {"interval_seconds": 10.0, "duration_ms": 1.0},
            "writer": {
                "queue_depth": 0,
                "dropped_snapshot_count": 0,
                "fault_count": 0,
                "non_finite_numeric_count": 0,
            },
            "read_metrics_ms": {"game": 0.1, "resources": 0.2},
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

    stage_offsets = {"origin": 0.0, "activation": 0.30, "forward": 0.60, "final": 0.90}

    def start_records(stage: str) -> list[dict[str, Any]]:
        offset = stage_offsets[stage]
        return [
            {
                **common(stage),
                "schema": "oni.event.v2",
                "event": "session_started",
                "reason": "save_loader_load_complete",
                "seq": 1,
                "ts": timestamp(offset),
            },
            {
                **common(stage),
                "schema": "oni.event.v2",
                "event": "save_loaded",
                "reason": "save_loader_load_complete",
                "seq": 2,
                "ts": timestamp(offset + 0.02),
            },
        ]

    def save_record(
        stage: str,
        next_stage: str,
        offset: float,
        *,
        seq: int = 6,
    ) -> dict[str, Any]:
        return {
            **common(next_stage),
            "schema": "oni.event.v2",
            "event": "save_write_completed",
            "session_id": session_ids[stage],
            "seq": seq,
            "ts": timestamp(offset),
            "success": True,
            "is_auto_save": False,
            "update_save_pointer": True,
            "file_rewrite_observed": True,
            "save_file_readable": True,
            "failure_reason": None,
            "requested_save_path_id": derivative_path_id,
            "result_save_path_id": derivative_path_id,
            "active_save_path_id": derivative_path_id,
            "before_save_id_hash": save_hashes[stage],
            "written_save_id_hash": save_hashes[next_stage],
            "active_save_id_hash": save_hashes[next_stage],
        }

    def save_started_record(stage: str, offset: float, *, seq: int = 5) -> dict[str, Any]:
        return {
            **common(stage),
            "schema": "oni.event.v2",
            "event": "save_write_started",
            "seq": seq,
            "ts": timestamp(offset),
            "is_auto_save": False,
            "update_save_pointer": True,
            "requested_save_path_id": derivative_path_id,
            "before_save_id_hash": save_hashes[stage],
            "save_dirty": True,
        }

    def save_control_record(stage: str, offset: float, *, seq: int = 4) -> dict[str, Any]:
        return {
            **common(stage),
            "schema": "oni.event.v2",
            "event": "validation_control_started",
            "seq": seq,
            "ts": timestamp(offset),
            "control_id": f"save-{stage}-{run_id}",
            "operation": "save_checkpoint",
            "test_run_id": run_id,
            "target_save_path_id": derivative_path_id,
        }

    def reload_control_record(
        stage: str,
        next_stage: str,
        offset: float,
        *,
        seq: int = 8,
    ) -> dict[str, Any]:
        return {
            **common(next_stage),
            "schema": "oni.event.v2",
            "event": "validation_control_started",
            "session_id": session_ids[stage],
            "seq": seq,
            "ts": timestamp(offset),
            "control_id": f"reload-{stage}-{run_id}",
            "operation": "reload_checkpoint",
            "test_run_id": run_id,
            "expected_save_path_id": derivative_path_id,
            "expected_save_id_hash": save_hashes[next_stage],
        }

    def unload_and_end_records(
        stage: str,
        next_stage: str,
        unload_offset: float,
        end_offset: float,
        *,
        first_seq: int = 9,
    ) -> list[dict[str, Any]]:
        return [
            {
                **common(next_stage),
                "schema": "oni.event.v2",
                "event": "save_unloaded",
                "reason": "save_loader_load_start",
                "session_id": session_ids[stage],
                "seq": first_seq,
                "ts": timestamp(unload_offset),
            },
            {
                **common(next_stage),
                "schema": "oni.event.v2",
                "event": "session_ended",
                "reason": "save_loader_load_start",
                "session_id": session_ids[stage],
                "seq": first_seq + 1,
                "ts": timestamp(end_offset),
            },
        ]

    origin_snapshot = state("origin", 3, 0.04, 1)
    origin_post_save = state(
        "origin",
        7,
        0.12,
        2,
        save_id_hash=save_hashes["activation"],
        active_save_path_id=derivative_path_id,
    )
    activation_snapshot = state("activation", 3, 0.34, 1)
    activation_post_save = state(
        "activation",
        7,
        0.42,
        2,
        save_id_hash=save_hashes["forward"],
        active_save_path_id=derivative_path_id,
    )
    forward_snapshot = state("forward", 3, 0.64, 1)
    forward_post_save = state(
        "forward",
        7,
        0.72,
        2,
        save_id_hash=save_hashes["final"],
        active_save_path_id=derivative_path_id,
    )
    raw_latest_state = state("final", 3, 0.94, 1)
    origin_projection = projection(origin_snapshot)
    activation_projection = projection(activation_snapshot)
    forward_projection = projection(forward_snapshot)
    final_projection = projection(raw_latest_state)
    final_snapshot_id = str(raw_latest_state["snapshot_id"])
    final_session_id = session_ids["final"]

    lineage = bridge / "evidence" / "save-lineage" / run_id / "manifest.json"
    origin_prefix = [*start_records("origin"), origin_snapshot]
    origin_archive = (
        bridge
        / "sessions"
        / shutdown_contract.safe_session_name(session_ids["origin"])
        / "state.jsonl"
    )
    origin_records = [
        *origin_prefix,
        save_control_record("origin", 0.06),
        save_started_record("origin", 0.08),
        save_record("origin", "activation", 0.10),
        origin_post_save,
        reload_control_record("origin", "activation", 0.14),
        *unload_and_end_records("origin", "activation", 0.16, 0.18),
    ]
    # Startup v3 authenticates both the minimal S0 prefix placed in the bundle
    # and the complete S0 source archive (including its save/reload suffix).
    # Publish the bundle only after that complete origin archive exists, and
    # before any later-session archive is created.
    write_jsonl(origin_archive, origin_records)

    loader_path_text = str(paths["loader"])
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
        f"validation_run_id={run_id} "
        f"startup_binding_hash={startup_binding_hash} "
        f"startup_intent_hash={startup_binding_document['startup_intent_hash']} "
        f"validation_process_id={validation_process_id} "
        f"requested_save_path_id={startup_binding_document['requested_loader_path_id_upper']} "
        f"active_save_path_id={startup_binding_document['requested_loader_path_id_upper']}\n"
    ).encode("utf-8")
    archive_prefix_payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in origin_prefix
    ).encode("utf-8")
    origin_source_identity = startup_contract.identity_id(origin_archive.lstat())
    startup_captures = {
        "runtime_config_evidence": startup_capture(runtime_config_payload, f"{run_id}-runtime"),
        "startup_binding": startup_capture(startup_binding_payload, f"{run_id}-binding"),
        "consumed_prefs": startup_capture(consumed_prefs_payload, f"{run_id}-prefs"),
        "player_log_prefix": startup_capture(player_log_payload, f"{run_id}-player"),
        "session_archive_prefix": startup_contract.Capture(
            archive_prefix_payload,
            startup_contract.sha256_bytes(archive_prefix_payload),
            len(archive_prefix_payload),
            origin_source_identity,
        ),
    }
    startup_material = startup_contract.validate_material(
        startup_captures,
        expected_runtime_sha256=sha256(runtime_config_path),
        expected_binding_sha256=startup_binding_hash,
        process_id=validation_process_id,
    )
    startup_process = startup_contract.ProcessEvidence(
        validation_process_id,
        "OxygenNotIncluded.exe",
        workflow.contract_canonical_hash({"process_creation": run_id}),
        workflow.contract_canonical_hash({"process_image_path": run_id}),
        workflow.contract_canonical_hash({"process_image": run_id}),
        "",
    )
    startup_source_payload = origin_archive.read_bytes()
    startup_source_archive = {
        "session_id": session_ids["origin"],
        "source_identity_id": origin_source_identity,
        "file_sha256": startup_contract.sha256_bytes(startup_source_payload),
        "byte_length": len(startup_source_payload),
        "record_count": len(origin_records),
        "first_seq": 1,
        "last_seq": len(origin_records),
        "record_chain_hash": workflow.contract_canonical_hash(
            [workflow.contract_canonical_hash(record) for record in origin_records]
        ),
        "prefix_record_count": len(origin_prefix),
        "prefix_file_sha256": startup_contract.sha256_bytes(archive_prefix_payload),
        "prefix_byte_length": len(archive_prefix_payload),
    }
    validated_origin_session, validated_prefix_count, _ = (
        startup_contract.validate_source_archive_records(
            origin_records,
            startup_binding_document,
            startup_binding_hash,
            validation_process_id,
        )
    )
    assert_true(
        validated_origin_session == session_ids["origin"]
        and validated_prefix_count == len(origin_prefix),
        "fixture complete S0 source archive did not retain the startup prefix boundary",
    )
    startup_report = startup_contract.build_report(
        startup_captures,
        startup_material,
        startup_process,
        startup_source_archive,
        expected_runtime_sha256=sha256(runtime_config_path),
        expected_binding_sha256=startup_binding_hash,
        created_at=timestamp(0.27),
    )
    startup_bundle = lineage.parent / shutdown_contract.STARTUP_BUNDLE_DIRECTORY
    lineage.parent.mkdir(parents=True, exist_ok=True)
    later_archive_paths = {
        stage: bridge
        / "sessions"
        / shutdown_contract.safe_session_name(session_ids[stage])
        / "state.jsonl"
        for stage in ("activation", "forward", "final")
    }
    assert_true(
        all(not path.exists() for path in later_archive_paths.values()),
        "later session archives existed before startup bundle publication",
    )
    startup_contract.publish_bundle(startup_bundle, startup_captures, startup_report)

    activation_records = [
        *start_records("activation"),
        activation_snapshot,
        save_control_record("activation", 0.36),
        save_started_record("activation", 0.38),
        save_record("activation", "forward", 0.40),
        activation_post_save,
        reload_control_record("activation", "forward", 0.44),
        *unload_and_end_records("activation", "forward", 0.46, 0.48),
    ]
    forward_session_records = [
        *start_records("forward"),
        forward_snapshot,
        save_control_record("forward", 0.66),
        save_started_record("forward", 0.68),
        save_record("forward", "final", 0.70),
        forward_post_save,
        reload_control_record("forward", "final", 0.74),
        *unload_and_end_records("forward", "final", 0.76, 0.78),
    ]
    final_records = [
        *start_records("final"),
        raw_latest_state,
        {
            **common("final"),
            "schema": "oni.event.v2",
            "event": "session_ended",
            "reason": "application_quit",
            "seq": 4,
            "ts": timestamp(0.98),
        },
        {
            **common("final"),
            "schema": "oni.event.v2",
            "event": "probe_shutdown",
            "seq": 5,
            "ts": timestamp(1.00),
        },
    ]
    for stage, records in (
        ("activation", activation_records),
        ("forward", forward_session_records),
        ("final", final_records),
    ):
        write_jsonl(later_archive_paths[stage], records)
    all_runtime_records = [
        *origin_records,
        *activation_records,
        *forward_session_records,
        *final_records,
    ]
    write_jsonl(bridge / "state.jsonl", all_runtime_records)
    latest_state_hash = canonical_state_hash(raw_latest_state)

    startup_expectations = {
        "validation_run_id": run_id,
        "validation_process_id": validation_process_id,
        "startup_binding_hash": startup_binding_hash,
        "startup_intent_hash": startup_binding_document["startup_intent_hash"],
        "runtime_config_evidence_sha256": sha256(runtime_config_path),
        "loader_path_id_upper": startup_binding_document[
            "requested_loader_path_id_upper"
        ],
        "loader_save_sha256": startup_binding_document[
            "requested_loader_save_sha256"
        ],
        "game_build": startup_binding_document["expected_game_build"],
        "mod_version": startup_binding_document["expected_mod_version"],
    }
    terminal_runtime = oni_bridge.ReplayState(
        startup_expectations=startup_expectations,
        require_startup_binding=True,
    )
    terminal_runtime.process_many(copy.deepcopy(all_runtime_records))
    terminal_output = pathlib.Path(fixture["case"]) / "terminal-publication"
    oni_bridge.write_outputs(terminal_output, terminal_runtime, replay_mode=False)
    latest_state = json.loads(
        (terminal_output / "latest_state.json").read_text(encoding="utf-8")
    )
    terminal_health = json.loads(
        (terminal_output / "health.json").read_text(encoding="utf-8")
    )
    enriched_core = copy.deepcopy(latest_state)
    enriched_core.pop("bridge", None)
    startup_health = (
        terminal_health.get("startup_binding")
        if isinstance(terminal_health.get("startup_binding"), Mapping)
        else {}
    )
    assert_true(
        enriched_core == raw_latest_state
        and terminal_health.get("status") == "session_ended"
        and terminal_health.get("trust_ready") is False
        and terminal_health.get("trust_blockers") == []
        and startup_health.get("verified") is True
        and startup_health.get("verified_session_id") == final_session_id
        and startup_health.get("requested_save_path_id")
        == startup_binding_document["requested_loader_path_id_upper"]
        and startup_health.get("current_active_save_path_id") == derivative_path_id
        and startup_health.get("current_save_id_hash") == save_hashes["final"]
        and startup_health.get("distinct_path_transition_count") == 1
        and startup_health.get("blockers") == [],
        "fixture four-session Bridge startup/terminal publication is not trusted",
    )
    write_bytes(bridge / "latest_state.json", (terminal_output / "latest_state.json").read_bytes())
    write_bytes(bridge / "health.json", (terminal_output / "health.json").read_bytes())

    forward = "cmd-priority-forward-fixture"
    inverse = "cmd-priority-inverse-fixture"
    (
        forward_bundle,
        forward_bundle_path,
        forward_outcome,
        forward_records,
        forward_certificate,
        forward_certificate_path,
    ) = make_command_bundle(bridge, forward, sequence=11)
    (
        inverse_bundle,
        inverse_bundle_path,
        inverse_outcome,
        inverse_records,
        inverse_certificate,
        inverse_certificate_path,
    ) = make_command_bundle(bridge, inverse, sequence=12)

    lineage_policy = {
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

    def save_projection(record: Mapping[str, Any]) -> dict[str, Any]:
        keys = {
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
        return {key: value for key, value in record.items() if key in keys}

    initial: dict[str, Any] = {
        "kind": "activation",
        "activated_at": timestamp(0.34),
        "previous_checkpoint_hash": None,
        "source": copy.deepcopy(origin_projection),
        "save": save_projection(save_record("origin", "activation", 0.10)),
        "reload": copy.deepcopy(activation_projection),
        "policy_gate": copy.deepcopy(lineage_policy),
        "checkpoint_hash": "",
    }
    initial["checkpoint_hash"] = shutdown_contract.lineage_contract.checkpoint_hash(initial)

    def artifact(index: int, step_id: str) -> dict[str, Any]:
        path = lineage.parent / "artifacts" / f"{index:02d}-{step_id}" / "fixture.txt"
        write_bytes(path, f"{run_id}:{step_id}\n".encode("utf-8"))
        return {
            "path": str(path),
            "sha256": sha256(path),
            "length": path.stat().st_size,
            "source_path_id": workflow.contract_canonical_hash(
                {"source": f"{run_id}:{step_id}"}
            ),
        }

    def checkpoint(
        index: int,
        step_id: str,
        before: Mapping[str, Any],
        reload_state: Mapping[str, Any],
        previous_hash: str,
        command_id: str,
        stage: str,
        next_stage: str,
        save_offset: float,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "index": index,
            "step_id": step_id,
            "scenario": f"{step_id} persists after explicit save and reload",
            "outcome": "confirmed",
            "expected_mutation": "persisted_change",
            "recorded_at": reload_state["snapshot_ts"],
            "previous_checkpoint_hash": previous_hash,
            "before": copy.deepcopy(dict(before)),
            "command": {"id": command_id, "action": "set_duplicant_chore_priority"},
            "artifacts": [artifact(index, step_id)],
            "save": save_projection(save_record(stage, next_stage, save_offset)),
            "reload": copy.deepcopy(dict(reload_state)),
            "policy_gate": copy.deepcopy(lineage_policy),
            "checkpoint_hash": "",
        }
        result["checkpoint_hash"] = shutdown_contract.lineage_contract.checkpoint_hash(result)
        return result

    forward_checkpoint = checkpoint(
        1,
        "priority_forward",
        activation_projection,
        forward_projection,
        initial["checkpoint_hash"],
        forward,
        "activation",
        "forward",
        0.40,
    )
    inverse_checkpoint = checkpoint(
        2,
        "priority_inverse",
        forward_projection,
        final_projection,
        forward_checkpoint["checkpoint_hash"],
        inverse,
        "forward",
        "final",
        0.70,
    )
    latest_checkpoint_hash = inverse_checkpoint["checkpoint_hash"]
    lineage_document = {
        "schema": "oni.test-save-lineage.v1",
        "test_run_id": run_id,
        "status": "active",
        "source_kind": "save_as_copy",
        "expected_mod_version": "0.7.0",
        "reserved_at": timestamp(0.15),
        "save_path": str(derivative),
        "save_path_id": workflow.path_id(derivative, contract="upper"),
        "save_scope": "external_test",
        "save_absent_at_reservation": True,
        "reservation_source": copy.deepcopy(origin_projection),
        "reservation_policy": copy.deepcopy(lineage_policy),
        "initial": initial,
        "checkpoints": [forward_checkpoint, inverse_checkpoint],
        "latest": copy.deepcopy(final_projection),
        "latest_checkpoint_hash": latest_checkpoint_hash,
    }
    write_json(lineage, lineage_document)

    def make_freeze(
        step_id: str,
        checkpoint_record: Mapping[str, Any],
        bundle: dict[str, Any],
        bundle_path: pathlib.Path,
        records: list[dict[str, Any]],
        certificate: dict[str, Any],
        certificate_path: pathlib.Path,
    ) -> tuple[dict[str, Any], pathlib.Path]:
        freeze: dict[str, Any] = {
            "schema": "oni.priority-command-lineage-freeze.v1",
            "step_id": step_id,
            "test_run_id": run_id,
            "manifest_binding": {
                "latest_checkpoint_hash": checkpoint_record["previous_checkpoint_hash"],
                "before": copy.deepcopy(checkpoint_record["before"]),
            },
            "approval_bundle": {
                "path": bundle_path.relative_to(bridge).as_posix(),
                "file_sha256": sha256(bundle_path),
                "bundle_hash": bundle["bundle_hash"],
                "document": bundle,
            },
            "confirmation_certificate": {
                "path": certificate_path.relative_to(bridge).as_posix(),
                "file_sha256": sha256(certificate_path),
                "outcomes_hash": certificate["outcomes_hash"],
                "document": certificate,
            },
            "confirmation_records": records,
            "frozen_at": "2026-08-30T21:02:00+00:00",
            "freeze_hash": "",
        }
        freeze["freeze_hash"] = workflow.contract_hash_without(freeze, "freeze_hash", "fixture freeze")
        freeze_path = lineage.parent / "priority-command-freezes" / f"{step_id}.json"
        write_json(freeze_path, freeze)
        return freeze, freeze_path

    forward_freeze, forward_freeze_path = make_freeze(
        "priority_forward",
        forward_checkpoint,
        forward_bundle,
        forward_bundle_path,
        forward_records,
        forward_certificate,
        forward_certificate_path,
    )
    inverse_freeze, inverse_freeze_path = make_freeze(
        "priority_inverse",
        inverse_checkpoint,
        inverse_bundle,
        inverse_bundle_path,
        inverse_records,
        inverse_certificate,
        inverse_certificate_path,
    )
    forward_frame_path = bridge / "decision-frames" / (
        "sha256-" + str(forward_bundle["source_frame"]["frame_id"]).removeprefix("sha256:") + ".json"
    )
    if evict_forward_frame:
        forward_frame_path.unlink()
    if evict_forward_certificate:
        forward_certificate_path.unlink()

    def report_command(
        step_id: str,
        command_id: str,
        bundle: dict[str, Any],
        bundle_path: pathlib.Path,
        freeze: dict[str, Any],
        freeze_path: pathlib.Path,
    ) -> dict[str, Any]:
        command_path = bridge / "commands" / "done" / f"{command_id}.json"
        frame_id = str(bundle["source_frame"]["frame_id"])
        frame_path = bridge / "decision-frames" / f"sha256-{frame_id.removeprefix('sha256:')}.json"
        return {
            "step_id": step_id,
            "id": command_id,
            "action": "set_duplicant_chore_priority",
            "args": bundle["final_command"]["args"],
            "file": command_path.relative_to(bridge).as_posix(),
            "file_sha256": sha256(command_path),
            "command_intent_hash": bundle["command_intent_hash"],
            "approval_receipt_hash": bundle["approval_receipt_hash"],
            "policy_hash": bundle["source_policy_hash"],
            "automatic_approval_bundle": {
                "path": bundle_path.relative_to(bridge).as_posix(),
                "file_sha256": sha256(bundle_path),
                "bundle_hash": bundle["bundle_hash"],
            },
            "lineage_freeze": {
                "path": str(freeze_path),
                "file_sha256": sha256(freeze_path),
                "freeze_hash": freeze["freeze_hash"],
            },
            "source_frame": {
                "frame_id": bundle["source_frame"]["frame_id"],
                "archive_file": frame_path.relative_to(bridge).as_posix(),
                "archive_file_sha256": bundle["source_frame"]["archive_file_sha256"],
                "archive_currently_present": frame_path.is_file(),
            },
            "confirmation": {
                "attestation_hash": bundle["final_command"]["confirmation"]["attestation_hash"],
                "ack_sequences": {
                    "accepted": bundle["final_command"]["accepted_ack_seq"],
                    "executed": bundle["final_command"]["executed_ack_seq"],
                    "local_effect_observed": bundle["final_command"]["local_effect_ack_seq"],
                },
                "witness": bundle["final_command"]["confirmation"]["witness"],
                "certificate": {
                    "path": freeze["confirmation_certificate"]["path"],
                    "file_sha256": freeze["confirmation_certificate"]["file_sha256"],
                    "outcomes_hash": freeze["confirmation_certificate"]["outcomes_hash"],
                },
            },
        }

    # Publish the terminal Bridge surface again after the synthetic immutable
    # command evidence exists, then use the production pair-time builder.  The
    # raw Probe log remains Bridge-free; only latest_state.json is enriched.
    oni_bridge.write_outputs(bridge, terminal_runtime, replay_mode=False)
    latest_state = json.loads((bridge / "latest_state.json").read_text(encoding="utf-8"))
    terminal_health = json.loads((bridge / "health.json").read_text(encoding="utf-8"))
    assert_true(
        workflow.priority_pair_contract.snapshot_core_without_bridge(latest_state)
        == raw_latest_state
        and terminal_health.get("status") == "session_ended",
        "fixture terminal publication changed its trusted raw tip",
    )
    pair_command_pipeline = workflow.priority_pair_contract._command_pipeline_evidence(
        bridge,
        latest_state,
        runtime_policy,
        raw_state=raw_latest_state,
    )

    pair_report: dict[str, Any] = {
        "schema": "oni.priority-pair-lineage-finalization.v1",
        "status": "passed",
        "classification": None,
        "verification_mode": "offline_read_only",
        "fail_closed": True,
        "manifest": {
            "path": str(lineage),
            "file_sha256": sha256(lineage),
            "test_run_id": run_id,
            "latest_checkpoint_hash": latest_checkpoint_hash,
        },
        "pair": {
            "forward_step": "priority_forward",
            "inverse_step": "priority_inverse",
            "commands": [
                report_command("priority_forward", forward, forward_bundle, forward_bundle_path, forward_freeze, forward_freeze_path),
                report_command("priority_inverse", inverse, inverse_bundle, inverse_bundle_path, inverse_freeze, inverse_freeze_path),
            ],
        },
        "final_tip": {
            "session_id": final_session_id,
            "snapshot_id": final_snapshot_id,
            "save_id_hash": sha256(derivative),
            "state_hash": latest_state_hash,
        },
        "shutdown": {
            "policy_file_sha256": sha256(bridge / "action-policy.json"),
            "actions_enabled": False,
            "allowed_save_hash_count": 0,
            "command_pipeline": pair_command_pipeline,
            "queues": {
                "nonterminal": {name: 0 for name in workflow.NONTERMINAL_QUEUES},
                "temporary_file_count": 0,
            },
        },
        "report_hash": "",
    }
    pair_report["report_hash"] = workflow.contract_hash_without(pair_report, "report_hash", "fixture pair report")
    pair_report_path = lineage.parent / "priority-pair-finalization.json"
    write_json(pair_report_path, pair_report)
    fixture["pair_report_hash"] = pair_report["report_hash"]
    fixture["forward_bundle_hash"] = forward_bundle["bundle_hash"]
    fixture["inverse_bundle_hash"] = inverse_bundle["bundle_hash"]
    event_log = later_archive_paths["final"]
    shutdown_document = shutdown_contract.build_shutdown_evidence(
        manifest_path=lineage,
        bridge_dir=bridge,
        pair_report_path=pair_report_path,
        expected_pair_report_hash=pair_report["report_hash"],
        expected_run_id=run_id,
        forward_step="priority_forward",
        inverse_step="priority_inverse",
        command_ids=[forward, inverse],
        command_bundle_hashes=[forward_bundle["bundle_hash"], inverse_bundle["bundle_hash"]],
        event_log=event_log,
        startup_bundle_dir=startup_bundle,
        expected_startup_evidence_hash=startup_report["evidence_hash"],
        expected_validation_process_id=validation_process_id,
    )
    shutdown_evidence_path = lineage.parent / "shutdown-evidence.json"
    shutdown_contract.write_evidence_create_only(shutdown_evidence_path, lineage, shutdown_document)
    fixture["shutdown_evidence_path"] = shutdown_evidence_path
    fixture["shutdown_evidence_hash"] = shutdown_document["evidence_hash"]
    fixture["startup_consumption_bundle"] = startup_bundle
    fixture["startup_consumption_evidence_hash"] = startup_report["evidence_hash"]
    fixture["validation_process_id"] = validation_process_id
    fixture["session_ids"] = session_ids
    fixture["save_hashes"] = save_hashes
    return {
        "bridge": bridge,
        "archived_old": archived_old,
        "derivative": derivative,
        "derivative_preview": derivative_preview,
        "lineage": lineage,
        "shutdown_evidence": shutdown_evidence_path,
        "shutdown_event_log": event_log,
        "final_event_log": event_log,
        "origin_archive": origin_archive,
        "session_ids": session_ids,
        "save_hashes": save_hashes,
        "startup_consumption_bundle": startup_bundle,
    }


def file_inventory(root: pathlib.Path) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (sha256(path), path.stat().st_mtime_ns)
    return result


def test_syntax(base: pathlib.Path) -> dict[str, Any]:
    del base
    with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as handle:
        compiled = pathlib.Path(handle.name)
    try:
        py_compile.compile(str(CORE), cfile=str(compiled), doraise=True)
        py_compile.compile(
            str(SCRIPTS / "configure_live_validation_runtime.py"),
            cfile=str(compiled),
            doraise=True,
        )
        py_compile.compile(str(SCRIPTS / "create_shutdown_evidence.py"), cfile=str(compiled), doraise=True)
        py_compile.compile(str(pathlib.Path(__file__).resolve()), cfile=str(compiled), doraise=True)
    finally:
        compiled.unlink(missing_ok=True)
    parser_command = (
        "$files=@('scripts/capture-live-safety-boundary.ps1','scripts/create-named-test-clone.ps1',"
        "'scripts/restore-named-test-clone.ps1');$all=@();foreach($f in $files){$t=$null;$e=$null;"
        "[Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $f).Path,"
        "[ref]$t,[ref]$e)|Out-Null;$all+=@($e)};if($all.Count){exit 1}"
    )
    run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", parser_command])
    parser = workflow.build_parser()
    finalize_parser = next(
        action for action in parser._actions if getattr(action, "choices", None)
    ).choices["finalize"]
    abort_parser = next(
        action for action in parser._actions if getattr(action, "choices", None)
    ).choices["abort"]
    required = {action.dest for action in finalize_parser._actions if action.required}
    required_runtime_config = {
        "runtime_config_evidence",
        "expected_runtime_config_evidence_sha256",
        "expected_startup_binding_sha256",
        "runtime_bootstrap_result",
        "expected_runtime_bootstrap_result_sha256",
        "release_dll",
        "expected_release_dll_sha256",
        "game_managed_dir",
    }
    required_startup_consumption = {
        "startup_consumption_bundle",
        "startup_consumption_evidence_hash",
        "validation_process_id",
    }
    assert_true(
        {
            "shutdown_evidence",
            "shutdown_evidence_hash",
            *required_runtime_config,
            *required_startup_consumption,
        }.issubset(required),
        "finalize parser does not require all external shutdown/startup/runtime-config trust anchors",
    )
    parser_test_options = {
        action.dest
        for action in finalize_parser._actions
        if action.dest.startswith("test_") and action.dest != "test_root"
    }
    assert_true(
        parser_test_options == set(workflow.FINALIZE_TEST_ONLY_OPTIONS),
        "finalize TestRoot-only parser options are not exhaustively classified by the live-mode guard",
    )
    abort_required = {action.dest for action in abort_parser._actions if action.required}
    required_abort_anchors = {
        "manifest",
        "expected_environment_manifest_sha256",
        "runtime_config_evidence",
        "expected_runtime_config_evidence_sha256",
        "expected_startup_binding_sha256",
        "runtime_bootstrap_result",
        "expected_runtime_bootstrap_result_sha256",
        "release_dll",
        "expected_release_dll_sha256",
        "game_managed_dir",
    }
    assert_true(
        required_abort_anchors.issubset(abort_required),
        "abort parser does not require every external manifest/config/Release/Managed anchor",
    )
    abort_destinations = {action.dest for action in abort_parser._actions}
    assert_true(
        required_startup_consumption.isdisjoint(abort_destinations),
        "pre-session abort unexpectedly accepts normal-completion startup certificate anchors",
    )
    abort_test_options = {
        action.dest
        for action in abort_parser._actions
        if action.dest.startswith("test_") and action.dest != "test_root"
    }
    assert_true(
        abort_test_options == set(workflow.ABORT_TEST_ONLY_OPTIONS),
        "abort TestRoot-only parser options are not exhaustively classified by the live-mode guard",
    )
    return {
        "py_compile": "passed",
        "powershell_parser_errors": 0,
        "required_shutdown_cli_anchors": ["shutdown_evidence", "shutdown_evidence_hash"],
        "required_startup_consumption_cli_anchors": sorted(required_startup_consumption),
        "abort_startup_certificate_anchors": "intentionally_absent_pre_session_only",
        "required_runtime_config_cli_anchors": sorted(required_runtime_config),
        "runtime_bootstrap_result_schema_key_count": len(
            runtime_config_contract.RUNTIME_BOOTSTRAP_RESULT_KEYS
        ),
        "guarded_testroot_only_finalize_options": sorted(parser_test_options),
        "required_abort_cli_anchors": sorted(required_abort_anchors),
        "guarded_testroot_only_abort_options": sorted(abort_test_options),
    }


def test_success_and_whatif(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "success")
    paths = fixture_paths(fixture)
    archive_before = workflow.snapshot_tree(pathlib.Path(fixture["oni"]) / "ai-bridge-archives")
    before_prepare = file_inventory(pathlib.Path(fixture["oni"]))
    plan = json.loads(core(fixture, "prepare", what_if=True).stdout)
    assert_true(plan.get("status") == "would_prepare", "prepare WhatIf status is wrong")
    assert_true(not paths["environment"].exists(), "prepare WhatIf wrote evidence")
    assert_true(file_inventory(pathlib.Path(fixture["oni"])) == before_prepare, "prepare WhatIf changed ONI fixture")
    prepare_fixture(fixture)
    runtime = simulate_runtime(fixture)
    runtime_before = file_inventory(pathlib.Path(fixture["oni"]))
    plan = json.loads(core(fixture, "finalize", what_if=True).stdout)
    assert_true(plan.get("status") == "would_finalize", "finalize WhatIf status is wrong")
    assert_true(
        plan.get("runtime_config_historical_verified") is True
        and plan.get("runtime_config_ordering_verified") is True
        and plan.get("runtime_config_evidence_sha256")
        == fixture["runtime_config_evidence_sha256"]
        and plan.get("expected_release_dll_sha256")
        == fixture["expected_release_dll_sha256"]
        and isinstance(plan.get("runtime_config_binding_hash"), str),
        "finalize WhatIf omitted the external runtime config/Release bindings",
    )
    assert_true(
        plan.get("startup_consumption_evidence_hash")
        == fixture["startup_consumption_evidence_hash"]
        and plan.get("validation_process_id") == fixture["validation_process_id"]
        and isinstance(plan.get("startup_certificate_projection_hash"), str)
        and isinstance(plan.get("startup_session_id"), str)
        and isinstance(plan.get("validation_process_creation_token"), str)
        and isinstance(plan.get("startup_archive_prefix_sha256"), str)
        and isinstance(plan.get("startup_archive_prefix_length"), int),
        "finalize WhatIf omitted startup certificate/PID/prefix custody anchors",
    )
    assert_true(
        not (paths["environment"] / "runtime-binding.json").exists(),
        "finalize WhatIf wrote runtime binding",
    )
    assert_true(file_inventory(pathlib.Path(fixture["oni"])) == runtime_before, "finalize WhatIf changed fixture")
    config_document = json.loads(
        pathlib.Path(fixture["runtime_config_evidence_path"]).read_text(encoding="utf-8")
    )
    runtime_result_document = json.loads(
        pathlib.Path(fixture["runtime_bootstrap_result"]).read_text(encoding="utf-8")
    )
    expected_runtime_result_projection = {
        "runtime_bootstrap_result_path_id_lower": workflow.path_id(
            pathlib.Path(fixture["runtime_bootstrap_result"]),
            contract="lower",
        ),
        "runtime_bootstrap_result_sha256": fixture[
            "expected_runtime_bootstrap_result_sha256"
        ],
        "runtime_bootstrap_result_hash": runtime_result_document["result_hash"],
        "runtime_bootstrap_transaction_id": runtime_result_document["transaction_id"],
        "installed_probe_tree_content_hash": runtime_result_document[
            "probe_tree_content_hash"
        ],
    }
    assert_true(
        {
            key: config_document["startup_intent"].get(key)
            for key in expected_runtime_result_projection
        }
        == expected_runtime_result_projection,
        "runtime config startup intent did not persist the exact runtime-result anchors",
    )
    assert_true(
        config_document["runtime"]["initial_disabled_policy"]["raw_sha256"]
        != sha256(runtime["bridge"] / "action-policy.json"),
        "fixture did not exercise legal post-config policy drift",
    )
    assert_true(
        sha256(paths["loader"]) != config_document["bindings"]["loader_save"]["state"]["sha256"],
        "fixture did not exercise legal loader drift",
    )
    result = json.loads(core(fixture, "finalize", apply=True).stdout)
    assert_true(result.get("status") == "finalized", "finalize did not succeed")
    assert_true(
        result.get("runtime_config_evidence_sha256")
        == fixture["runtime_config_evidence_sha256"]
        and result.get("expected_release_dll_sha256")
        == fixture["expected_release_dll_sha256"]
        and result.get("runtime_config_binding_hash") == plan["runtime_config_binding_hash"]
        and isinstance(result.get("runtime_config"), dict),
        "finalize result omitted or changed its runtime config binding",
    )
    compact_runtime_result_projection = {
        key: result["runtime_config"]["startup_intent"].get(key)
        for key in expected_runtime_result_projection
    }
    runtime_binding = json.loads(
        (paths["environment"] / "runtime-binding.json").read_text(encoding="utf-8")
    )
    assert_true(
        compact_runtime_result_projection == expected_runtime_result_projection
        and {
            key: runtime_binding["runtime_config"]["startup_intent"].get(key)
            for key in expected_runtime_result_projection
        }
        == expected_runtime_result_projection
        and result.get("runtime_config_binding_hash")
        == plan.get("runtime_config_binding_hash"),
        "runtime-result anchors did not persist from WhatIf through committed binding/result",
    )
    assert_true(
        result.get("schema") == workflow.FINALIZE_RESULT_SCHEMA
        and result.get("startup_consumption_evidence_hash")
        == fixture["startup_consumption_evidence_hash"]
        and result.get("validation_process_id") == fixture["validation_process_id"]
        and result.get("startup_session_id") == plan["startup_session_id"]
        and result.get("validation_process_creation_token")
        == plan["validation_process_creation_token"]
        and result.get("startup_archive_prefix_sha256")
        == plan["startup_archive_prefix_sha256"]
        and result.get("startup_archive_prefix_length")
        == plan["startup_archive_prefix_length"],
        "finalize result omitted or changed startup certificate/PID/prefix custody anchors",
    )
    assert_true(not runtime["derivative"].exists() and not runtime["derivative_preview"].exists(), "derivative remained")
    assert_true(sha256(paths["loader"]) == sha256(pathlib.Path(fixture["source"])), "loader save not restored")
    assert_true(
        sha256(paths["loader_preview"]) == sha256(pathlib.Path(fixture["source_preview"])),
        "loader preview not restored",
    )
    assert_true((pathlib.Path(fixture["oni"]) / "ai-bridge" / "old-runtime-marker.txt").is_file(), "old bridge missing")
    assert_true(
        (paths["environment"] / "rb").is_dir(),
        "new runtime bridge was not preserved",
    )
    workflow.verify_tree(
        pathlib.Path(fixture["oni"]) / "ai-bridge-archives",
        archive_before,
        "verifier preexisting archive",
    )
    comparison = json.loads(
        (paths["environment"] / "finalization" / "post-boundary-comparison.json").read_text(encoding="utf-8")
    )
    assert_true(comparison.get("status") == "passed", "post-boundary comparison did not pass")
    assert_true(len(comparison.get("save_diff", {}).get("added", [])) == 2, "post compare allowed more than loader pair")
    frozen_config = paths["environment"] / "finalization" / "pair" / "config.json"
    runtime_binding = json.loads(
        (paths["environment"] / "runtime-binding.json").read_text(encoding="utf-8")
    )
    assert_true(
        sha256(frozen_config) == fixture["runtime_config_evidence_sha256"]
        and frozen_config.read_bytes()
        == pathlib.Path(fixture["runtime_config_evidence_path"]).read_bytes(),
        "finalizer did not freeze the exact externally anchored runtime config evidence",
    )
    assert_true(
        runtime_binding.get("runtime_config") == result["runtime_config"]
        and result["runtime_config"].get("verification_report_hash")
        == fixture["runtime_config_verification_report_hash"],
        "runtime binding does not preserve the independent config verification report",
    )
    startup_record = runtime_binding.get("startup_consumption")
    assert_true(
        isinstance(startup_record, dict)
        and startup_record.get("evidence_hash") == fixture["startup_consumption_evidence_hash"]
        and startup_record.get("validation_process_id") == fixture["validation_process_id"]
        and startup_record.get("certificate_hash")
        == result["startup_certificate_projection_hash"]
        and startup_record.get("bundle_custody_hash")
        == result["startup_bundle_custody_hash"],
        "runtime binding lost startup certificate/custody anchors",
    )
    startup_current = pathlib.Path(fixture["startup_consumption_bundle"])
    startup_archived = paths["environment"] / "rb" / pathlib.Path(startup_record["relative"])
    assert_true(
        not startup_current.exists() and startup_archived.is_dir(),
        "startup bundle did not move into unique durable runtime custody",
    )
    journal_records = [
        json.loads(line)
        for line in (paths["environment"] / "restore-journal.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    terminal = next(
        (record for record in reversed(journal_records) if record.get("event") == "environment_finalized"),
        None,
    )
    terminal_data = terminal.get("data") if isinstance(terminal, dict) else None
    assert_true(
        isinstance(terminal_data, dict)
        and terminal_data.get("normal_shutdown_evidence_hash")
        == result["normal_shutdown_evidence_hash"]
        and terminal_data.get("startup_consumption_evidence_hash")
        == result["startup_consumption_evidence_hash"]
        and terminal_data.get("startup_session_id") == result["startup_session_id"]
        and terminal_data.get("validation_process_creation_token")
        == result["validation_process_creation_token"]
        and terminal_data.get("startup_archive_prefix_sha256")
        == result["startup_archive_prefix_sha256"]
        and terminal_data.get("startup_archive_prefix_length")
        == result["startup_archive_prefix_length"],
        "terminal finalize journal did not repeat the exact shutdown/startup custody anchors",
    )
    result_path = paths["environment"] / "finalization" / "finalize-result.json"
    result_raw_sha256 = sha256(result_path)
    replay_plan = json.loads(core(fixture, "finalize", what_if=True).stdout)
    assert_true(
        replay_plan.get("status") == "would_fresh_revalidate_existing_result"
        and replay_plan.get("fresh_post_boundary_required") is True
        and replay_plan.get("stored_result_authorizes_return") is False
        and replay_plan.get("physical_transaction_replayed") is False,
        "completed finalize WhatIf did not require fresh non-replay validation",
    )
    replay_second = json.loads(core(fixture, "finalize", apply=True).stdout)
    replay_third = json.loads(core(fixture, "finalize", apply=True).stdout)
    transition = json.loads(
        (
            paths["environment"]
            / "finalization"
            / "revalidation-passed.json"
        ).read_text(encoding="utf-8")
    )
    replay_journal = workflow.verify_journal(
        paths["environment"] / "restore-journal.jsonl"
    )
    assert_true(
        replay_second == result
        and replay_third == result
        and sha256(result_path) == result_raw_sha256
        and transition.get("source_recovery_sha256") is None
        and transition.get("source_recovery_state")
        == workflow.IMPLICIT_POST_COMMIT_REVALIDATION_STATE
        and sum(
            entry.get("event") == "restore_transaction_physical_committed"
            for entry in replay_journal
        )
        == 1,
        "completed finalize second/third replay was not stable and non-replaying",
    )
    return {
        "prepare_whatif": "no_mutation",
        "finalize_whatif": "no_mutation",
        "runtime_bootstrap_whatif": "zero_write_external_stage_anchors_persisted",
        "runtime_bootstrap_result_key_count": len(runtime_result_document),
        "runtime_bootstrap_anchor_projection_count": len(
            expected_runtime_result_projection
        ),
        "runtime_bootstrap_anchor_persistence": "whatif_to_binding_to_result",
        "active_parent_finalize_lock": "authentic_finalize_apply_accepted",
        "recursive_probe_restore": "passed",
        "old_bridge_restore": "passed",
        "preexisting_archive_tree": "hash_and_timestamp_exact",
        "derivative_pair": "recoverably_archived",
        "post_compare_allowed_pairs": 1,
        "runtime_config_evidence_frozen_exact": True,
        "runtime_config_binding": "exact_and_self_consistent",
        "release_dll_external_anchor": result["expected_release_dll_sha256"],
        "current_prefs_mods_policy_loader_drift": "historical_validation_passed",
        "configured_at_equal_shutdown_session_start": "accepted_by_no_later_than_contract",
        "completed_second_third_replay": "fresh_idempotent_nonreplay",
        "implicit_source_state": workflow.IMPLICIT_POST_COMMIT_REVALIDATION_STATE,
    }


def test_finalize_archive_absent_and_max_path(base: pathlib.Path) -> dict[str, Any]:
    absent = make_fixture(
        base,
        "finalize-archive-absent",
        run_id="g0-finalize-archive-absent",
        with_archive_root=False,
    )
    archive_root = pathlib.Path(absent["oni"]) / "ai-bridge-archives"
    assert_true(not archive_root.exists(), "archive-absent fixture unexpectedly created the archive root")
    prepare_fixture(absent)
    simulate_runtime(absent)
    absent_plan = json.loads(core(absent, "finalize", what_if=True).stdout)
    absent_result = json.loads(core(absent, "finalize", apply=True).stdout)
    assert_true(
        absent_plan.get("status") == "would_finalize"
        and absent_result.get("status") == "finalized"
        and not archive_root.exists(),
        "finalize did not restore an originally absent archive root exactly",
    )

    old_bridge_relatives = (
        "action-policy.json",
        "commands",
        "commands/approved",
        "commands/approving",
        "commands/confirming",
        "commands/done",
        "commands/executing",
        "commands/failed",
        "commands/pending",
        "commands/quarantine",
        "commands/rejected",
        "old-runtime-marker.txt",
        "validation-control-policy.json",
        "validation-controls",
        "validation-controls/approved",
        "validation-controls/done",
        "validation-controls/failed",
        "validation-controls/inflight",
        "validation-controls/quarantine",
    )
    old_probe_relatives = ("nested", "nested/contract.json", "OniAiProbeMod.dll")
    selected: tuple[str, str, int] | None = None
    for padding in range(1, 60):
        name = "fm-" + "x" * padding
        run_id = "g0-fm-" + "y" * padding
        case = base / name
        projected_roots = runtime_bootstrap_contract.Roots(
            run_id=run_id,
            oni_root=case / "oni-user",
            evidence_root=case / "evidence",
            test_root=case,
        )
        archive_name = f"{run_id}-preexisting-rtb-" + "0" * 24
        projected_plan = {
            "desired": {
                "bootstrap": {
                    "bridge_dir": str(case / "oni-user" / "ai-bridge"),
                },
                "archive_relative": f"ai-bridge-archives/{archive_name}",
            },
            "prestate": {
                "old_bridge": {
                    "snapshot": {
                        "entries": [
                            {"relative": relative} for relative in old_bridge_relatives
                        ]
                    }
                },
                "probe": {
                    "snapshot": {
                        "entries": [
                            {"relative": relative} for relative in old_probe_relatives
                        ]
                    }
                },
            },
        }
        units = max(
            runtime_bootstrap_contract.utf16_units(path)
            for path in runtime_bootstrap_contract.legacy_path_candidates(
                projected_roots,
                projected_plan,
            )
        )
        if units <= RUNTIME_BOOTSTRAP_LEGACY_MAX_UTF16_UNITS:
            selected = (name, run_id, units)
    assert_true(selected is not None, "could not construct a below-MAX_PATH finalize fixture")
    name, run_id, predicted_units = selected
    max_path = make_fixture(base, name, run_id=run_id)
    prepare_fixture(max_path)
    simulate_runtime(max_path)
    install_budget = runtime_bootstrap_install_path_budget(max_path)
    assert_true(
        install_budget["max_utf16_units"] == predicted_units
        and install_budget["max_utf16_units"]
        <= RUNTIME_BOOTSTRAP_LEGACY_MAX_UTF16_UNITS,
        "near-MAX_PATH fixture projection did not match the full install candidate set",
    )
    max_plan = json.loads(core(max_path, "finalize", what_if=True).stdout)
    max_result = json.loads(core(max_path, "finalize", apply=True).stdout)
    assert_true(
        max_plan.get("status") == "would_finalize" and max_result.get("status") == "finalized",
        "compact startup custody failed within the legacy Win32 path budget",
    )
    residuals = [
        path
        for path in pathlib.Path(max_path["case"]).rglob("*")
        if any(marker in path.name.casefold() for marker in (".tmp", ".claim", ".release-"))
        and not is_persistent_runtime_bootstrap_environment_lock_claim(path, max_path)
    ]
    assert_true(not residuals, "near-MAX_PATH finalize left temporary publication artifacts")
    actual_longest = max(len(str(path)) for path in pathlib.Path(max_path["case"]).rglob("*"))
    return {
        "original_archive_root_absent": "restored_exactly_absent",
        "startup_transient_predicted_utf16_units": predicted_units,
        "final_tree_longest_path_units": actual_longest,
        "runtime_bootstrap_legacy_limit": RUNTIME_BOOTSTRAP_LEGACY_MAX_UTF16_UNITS,
        "environment_legacy_limit": workflow.MAX_LEGACY_PATH_UTF16_UNITS,
        "temporary_artifact_count": 0,
    }


def test_loader_colony_isolation_refusals(base: pathlib.Path) -> dict[str, Any]:
    """Review P0-1: prepare accepts only a loader clone in its own colony folder."""
    results: dict[str, str] = {}
    fixture = make_fixture(base, "isolation-sibling", run_id="g0-isolation-sibling")
    paths = fixture_paths(fixture)
    sibling = pathlib.Path(fixture["save_dir"]) / paths["loader"].name
    shutil.copy2(paths["loader"], sibling)
    shutil.copy2(paths["loader_preview"], sibling.with_suffix(".png"))
    blocked = json.loads(
        core(fixture, "prepare", "--loader-save", str(sibling), what_if=True, expected={2}).stdout
    )
    assert_true("own colony folder" in str(blocked.get("error")), "loader in the source colony folder was not refused as non-isolated")
    results["loader_in_source_colony_folder"] = "refused"

    legacy = make_fixture(base, "isolation-legacy", run_id="g0-isolation-legacy")
    clone_path = fixture_paths(legacy)["clone"]
    clone = json.loads(clone_path.read_text(encoding="utf-8-sig"))
    del clone["target"]["isolation"]
    write_json(clone_path, clone)
    blocked = json.loads(core(legacy, "prepare", what_if=True, expected={2}).stdout)
    assert_true("isolated non-autosave clone" in str(blocked.get("error")), "legacy clone manifest was not refused as non-isolated")
    results["manifest_without_isolation"] = "refused"

    occupied = make_fixture(base, "isolation-occupied", run_id="g0-isolation-occupied")
    occupied_paths = fixture_paths(occupied)
    # A pre-boundary that already saw saves in the loader folder: the clone
    # folder was not new when the run started.
    loader_dir = occupied_paths["loader"].parent
    stray = loader_dir / "stray.sav"
    stray.write_bytes(b"stray")
    stray.with_suffix(".png").write_bytes(b"stray-preview")
    parked = pathlib.Path(occupied["case"]) / "parked-loader"
    parked.mkdir()
    for path in (occupied_paths["loader"], occupied_paths["loader_preview"]):
        os.replace(path, parked / path.name)
    occupied_paths["pre"].unlink()
    capture_pre(occupied)
    for path in (occupied_paths["loader"], occupied_paths["loader_preview"]):
        os.replace(parked / path.name, path)
    blocked = json.loads(core(occupied, "prepare", what_if=True, expected={2}).stdout)
    assert_true(
        "loader colony folder already held saves" in str(blocked.get("error")),
        "pre-boundary with an occupied loader folder was not refused",
    )
    results["loader_folder_seen_in_pre_boundary"] = "refused"
    return results


def next_run_fixture(
    fixture: dict[str, pathlib.Path | str], run_id: str
) -> dict[str, pathlib.Path | str]:
    """A later run on the same ONI root and evidence root, with its own pre and clone."""
    later = dict(fixture)
    later["run_id"] = run_id
    later["loader_stem"] = f"{run_id}-loader"
    capture_pre(later)
    create_loader_clone(later)
    return later


def test_golden_baseline_cross_run(base: pathlib.Path) -> dict[str, Any]:
    results: dict[str, str] = {}
    fixture = make_fixture(base, "golden-drift", with_extra_save_pair=True)
    golden = workflow.golden_baseline_path(pathlib.Path(fixture["evidence"]))
    golden_before = sha256(golden)
    stable = next_run_fixture(fixture, "g0-golden-stable")
    plan = json.loads(core(stable, "prepare", what_if=True).stdout)
    assert_true(plan.get("golden_baseline") == "verified", "unchanged originals did not verify")
    results["unchanged_originals_next_run"] = "verified"

    # An original save lost between runs (as the Cycle 6 autosave was on
    # 2026-09-13) must not become the next run's baseline.
    pathlib.Path(fixture["extra_save"]).unlink()
    pathlib.Path(fixture["extra_preview"]).unlink()
    drifted = next_run_fixture(fixture, "g0-golden-drifted")
    before = file_inventory(pathlib.Path(fixture["evidence"]))
    blocked = json.loads(core(drifted, "prepare", apply=True, expected={2}).stdout)
    assert_true("drifted from the golden baseline" in str(blocked.get("error")), "missing original was re-baselined")
    assert_true(file_inventory(pathlib.Path(fixture["evidence"])) == before, "drift refusal wrote evidence")
    assert_true(sha256(golden) == golden_before, "drift refusal replaced the golden baseline")
    results["missing_original_next_run"] = "refused_zero_write"

    changed = make_fixture(base, "golden-changed")
    write_bytes(pathlib.Path(changed["source_preview"]).with_name("Original.png"), b"changed-original-preview")
    changed_later = next_run_fixture(changed, "g0-golden-changed")
    blocked = json.loads(core(changed_later, "prepare", what_if=True, expected={2}).stdout)
    assert_true("drifted from the golden baseline" in str(blocked.get("error")), "changed original was re-baselined")
    results["changed_original_next_run"] = "refused"

    blocked = json.loads(
        core(changed_later, "prepare", "--establish-golden-baseline", what_if=True, expected={2}).stdout
    )
    assert_true("already exists" in str(blocked.get("error")), "existing golden baseline was replaced")
    results["re_establish_existing"] = "refused"

    fresh = make_fixture(base, "golden-fresh", establish_golden=False)
    blocked = json.loads(core(fresh, "prepare", what_if=True, expected={2}).stdout)
    assert_true("no golden baseline" in str(blocked.get("error")), "prepare ran without a golden baseline")
    fresh_golden = workflow.golden_baseline_path(pathlib.Path(fresh["evidence"]))
    assert_true(not fresh_golden.exists(), "refusal wrote a golden baseline")
    result = json.loads(core(fresh, "prepare", "--establish-golden-baseline", apply=True).stdout)
    assert_true(result.get("golden_baseline") == "established", "explicit establishment failed")
    manifest = json.loads(fixture_paths(fresh)["manifest"].read_text(encoding="utf-8-sig"))
    binding = manifest["bindings"]["golden_baseline"]
    assert_true(
        binding["sha256"] == sha256(fresh_golden) and binding["established_by_this_run"] is True,
        "environment manifest does not bind the golden baseline",
    )
    results["explicit_establishment"] = "bound_in_manifest"
    return results


def test_tamper_refusals(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "tamper-backup")
    prepare_fixture(fixture)
    simulate_runtime(fixture)
    before = file_inventory(pathlib.Path(fixture["oni"]))
    backup = fixture_paths(fixture)["environment"] / "backup" / "config" / "kplayerprefs.yaml"
    write_bytes(backup, b"tampered-backup")
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "tampered backup was accepted")
    assert_true(file_inventory(pathlib.Path(fixture["oni"])) == before, "tamper refusal changed fixture")

    clone_fixture = make_fixture(base, "tamper-clone", run_id="g0-clone-tamper")
    prepare_fixture(clone_fixture)
    simulate_runtime(clone_fixture)
    clone_path = fixture_paths(clone_fixture)["clone"]
    clone = json.loads(clone_path.read_text(encoding="utf-8"))
    clone["target"]["save_sha256"] = "sha256:" + "0" * 64
    write_json(clone_path, clone)
    blocked = json.loads(core(clone_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "tampered clone manifest was accepted")
    return {"backup_tamper": "infra_fail_refused", "clone_manifest_tamper": "infra_fail_refused"}


def test_priority_evidence_tamper_refusals(base: pathlib.Path) -> dict[str, Any]:
    report_fixture = make_fixture(base, "tamper-pair-report", run_id="g0-pair-report-tamper")
    prepare_fixture(report_fixture)
    report_runtime = simulate_runtime(report_fixture)
    report_path = report_runtime["lineage"].parent / "priority-pair-finalization.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["pair"]["tampered_after_publication"] = True
    report["report_hash"] = workflow.contract_hash_without(report, "report_hash", "tampered report")
    write_json(report_path, report)
    before_report_refusal = file_inventory(pathlib.Path(report_fixture["oni"]))
    blocked = json.loads(core(report_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "rehashed pair-report tamper was accepted")
    assert_true(
        file_inventory(pathlib.Path(report_fixture["oni"])) == before_report_refusal,
        "pair-report refusal changed the fixture",
    )

    bundle_fixture = make_fixture(base, "tamper-command-bundle", run_id="g0-bundle-tamper")
    prepare_fixture(bundle_fixture)
    simulate_runtime(bundle_fixture)
    command_id = "cmd-priority-forward-fixture"
    digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    bundle_path = pathlib.Path(bundle_fixture["oni"]) / "ai-bridge" / "confirmed-command-evidence" / digest / "approval-bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["final_command"]["result"]["tampered_after_publication"] = True
    bundle["final_command_hash"] = workflow.contract_canonical_hash(bundle["final_command"])
    bundle["bundle_hash"] = workflow.contract_hash_without(bundle, "bundle_hash", "tampered bundle")
    write_json(bundle_path, bundle)
    before_bundle_refusal = file_inventory(pathlib.Path(bundle_fixture["oni"]))
    blocked = json.loads(core(bundle_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "rehashed command-bundle tamper was accepted")
    assert_true(
        file_inventory(pathlib.Path(bundle_fixture["oni"])) == before_bundle_refusal,
        "command-bundle refusal changed the fixture",
    )

    freeze_fixture = make_fixture(base, "tamper-lineage-freeze", run_id="g0-freeze-tamper")
    prepare_fixture(freeze_fixture)
    freeze_runtime = simulate_runtime(freeze_fixture)
    freeze_path = freeze_runtime["lineage"].parent / "priority-command-freezes" / "priority_forward.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze["confirmation_records"].append({"kind": "tampered"})
    freeze["freeze_hash"] = workflow.contract_hash_without(freeze, "freeze_hash", "tampered freeze")
    write_json(freeze_path, freeze)
    blocked = json.loads(core(freeze_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "rehashed lineage-freeze tamper was accepted")
    return {
        "rehashed_pair_report": "expected_hash_mismatch_refused",
        "rehashed_command_bundle": "expected_hash_or_freeze_mismatch_refused",
        "rehashed_lineage_freeze": "pair_report_cross_binding_refused",
    }


def test_shutdown_evidence_finalizer_refusals(base: pathlib.Path) -> dict[str, Any]:
    process_zero_fixture = make_fixture(base, "shutdown-process-zero", run_id="g0-shutdown-process-zero")
    prepare_fixture(process_zero_fixture)
    process_zero_runtime = simulate_runtime(process_zero_fixture)
    process_zero_runtime["shutdown_evidence"].unlink()
    before_missing = file_inventory(pathlib.Path(process_zero_fixture["oni"]))
    blocked = json.loads(core(process_zero_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "zero processes without shutdown evidence were accepted")
    assert_true(
        file_inventory(pathlib.Path(process_zero_fixture["oni"])) == before_missing,
        "missing shutdown evidence refusal changed the fixture",
    )

    rehash_fixture = make_fixture(base, "shutdown-rehash", run_id="g0-shutdown-rehash")
    prepare_fixture(rehash_fixture)
    rehash_runtime = simulate_runtime(rehash_fixture)
    evidence_path = rehash_runtime["shutdown_evidence"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["final_tip"]["world_id"] = 99
    evidence["evidence_hash"] = workflow.contract_hash_without(
        evidence, "evidence_hash", "tampered shutdown evidence"
    )
    write_json(evidence_path, evidence)
    rehash_fixture["shutdown_evidence_hash"] = evidence["evidence_hash"]
    before_rehash = file_inventory(pathlib.Path(rehash_fixture["oni"]))
    blocked = json.loads(core(rehash_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "rehashed shutdown evidence cross-binding tamper was accepted")
    assert_true(
        file_inventory(pathlib.Path(rehash_fixture["oni"])) == before_rehash,
        "rehashed shutdown evidence refusal changed the fixture",
    )

    source_fixture = make_fixture(base, "shutdown-source", run_id="g0-shutdown-source")
    prepare_fixture(source_fixture)
    source_runtime = simulate_runtime(source_fixture)
    records = [
        json.loads(line)
        for line in source_runtime["shutdown_event_log"].read_text(encoding="utf-8").splitlines()
    ]
    records[-1]["mod_version"] = "tampered-after-publication"
    write_jsonl(source_runtime["shutdown_event_log"], records)
    before_source = file_inventory(pathlib.Path(source_fixture["oni"]))
    blocked = json.loads(core(source_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "changed shutdown source log was accepted")
    assert_true(
        file_inventory(pathlib.Path(source_fixture["oni"])) == before_source,
        "changed shutdown source refusal changed the fixture",
    )
    return {
        "only_process_counts_zero": "insufficient_without_evidence",
        "rehash_with_updated_cli_anchor": "cross_binding_refused",
        "source_log_file_hash_changed": "reconstruction_refused",
    }


def create_directory_junction(link: pathlib.Path, target: pathlib.Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    command = (
        "& { param([string]$Link,[string]$Target) "
        "New-Item -ItemType Junction -Path $Link -Target $Target -ErrorAction Stop | Out-Null }"
    )
    run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            str(link),
            str(target),
        ]
    )


def test_startup_consumption_preflight_custody_refusals(base: pathlib.Path) -> dict[str, Any]:
    results: dict[str, str] = {}

    missing, _ = ready_runtime_fixture(base, "sb-missing")
    missing_bundle = pathlib.Path(missing["startup_consumption_bundle"])
    os.replace(missing_bundle, missing_bundle.with_name("startup-consumption-evidence.missing"))
    assert_runtime_config_finalize_blocked(missing, "missing startup consumption bundle")
    results["missing_bundle"] = "refused_zero_write"

    wrong_path, _ = ready_runtime_fixture(base, "sb-wrong-path")
    wrong_path["startup_consumption_bundle"] = pathlib.Path(
        wrong_path["startup_consumption_bundle"]
    ).with_name("wrong-startup-consumption-evidence")
    assert_runtime_config_finalize_blocked(wrong_path, "wrong startup consumption logical path")
    results["wrong_logical_path"] = "refused_zero_write"

    wrong_hash, _ = ready_runtime_fixture(base, "sb-wrong-hash")
    wrong_hash["startup_consumption_evidence_hash"] = "sha256:" + "a" * 64
    assert_runtime_config_finalize_blocked(wrong_hash, "wrong startup consumption external hash")
    results["wrong_external_hash"] = "refused_zero_write"

    wrong_pid, _ = ready_runtime_fixture(base, "sb-wrong-pid")
    wrong_pid["validation_process_id"] = int(wrong_pid["validation_process_id"]) + 1
    assert_runtime_config_finalize_blocked(wrong_pid, "wrong startup validation PID")
    results["wrong_validation_pid"] = "refused_zero_write"

    hardlink, _ = ready_runtime_fixture(base, "sb-hardlink")
    hardlink_bundle = pathlib.Path(hardlink["startup_consumption_bundle"])
    hardlink_source = hardlink_bundle / startup_contract.REPORT_FILE
    os.link(hardlink_source, hardlink_bundle.parent / "startup-report-hardlink-alias.json")
    assert_runtime_config_finalize_blocked(hardlink, "hardlinked startup bundle report")
    results["hardlinked_bundle_file"] = "refused_zero_write"

    duplicate, duplicate_runtime = ready_runtime_fixture(base, "sb-duplicate")
    duplicate_bundle = pathlib.Path(duplicate["startup_consumption_bundle"])
    duplicate_relative = duplicate_bundle.relative_to(duplicate_runtime["bridge"])
    duplicate_archived = fixture_paths(duplicate)["environment"] / "rb" / duplicate_relative
    shutil.copytree(duplicate_bundle, duplicate_archived)
    assert_runtime_config_finalize_blocked(duplicate, "duplicate current/archived startup custody")
    results["current_archived_duplicate"] = "refused_zero_write"

    old_duplicate, old_runtime = ready_runtime_fixture(base, "sb-old-dup")
    old_bundle = pathlib.Path(old_duplicate["startup_consumption_bundle"])
    old_relative = old_bundle.relative_to(old_runtime["bridge"])
    shutil.copytree(old_bundle, old_runtime["archived_old"] / old_relative)
    assert_runtime_config_finalize_blocked(old_duplicate, "restored old bridge startup custody collision")
    results["old_bridge_duplicate"] = "refused_zero_write"

    for fixture_token, key in (
        ("sd-oa-old", "origin_archive"),
        ("sd-fl-old", "final_event_log"),
    ):
        log_duplicate, log_runtime = ready_runtime_fixture(
            base,
            fixture_token,
        )
        source = pathlib.Path(log_runtime[key])
        relative = source.relative_to(log_runtime["bridge"])
        duplicate_path = log_runtime["archived_old"] / relative
        write_bytes(duplicate_path, source.read_bytes())
        assert_runtime_config_finalize_blocked(
            log_duplicate,
            f"restored old bridge duplicate {key}",
        )
        results[f"old_bridge_duplicate_{key}"] = "refused_zero_write"

    reparse, _ = ready_runtime_fixture(base, "sb-reparse")
    reparse_bundle = pathlib.Path(reparse["startup_consumption_bundle"])
    reparse_target = reparse_bundle.with_name("startup-consumption-evidence.reparse-target")
    os.replace(reparse_bundle, reparse_target)
    create_directory_junction(reparse_bundle, reparse_target)
    assert_runtime_config_finalize_blocked(reparse, "reparse startup bundle")
    results["bundle_reparse"] = "refused_zero_write"
    return results


def make_committed_finalize_recovery_fixture(
    base: pathlib.Path,
    name: str,
) -> tuple[dict[str, pathlib.Path | str], pathlib.Path, pathlib.Path]:
    fixture = make_fixture(base, name, run_id=f"g0-{name}")
    prepare_fixture(fixture)
    simulate_runtime(fixture)
    blocked = json.loads(
        core(
            fixture,
            "finalize",
            "--test-finalize-result-fail-after-verify",
            apply=True,
            expected={2},
        ).stdout
    )
    assert_true(blocked.get("status") == "blocked", f"{name} did not reach committed recovery")
    environment = fixture_paths(fixture)["environment"]
    runtime_binding = json.loads((environment / "runtime-binding.json").read_text(encoding="utf-8"))
    archived_bundle = environment / "rb" / pathlib.Path(
        runtime_binding["startup_consumption"]["relative"]
    )
    assert_true(
        archived_bundle.is_dir()
        and not pathlib.Path(fixture["startup_consumption_bundle"]).exists(),
        f"{name} startup bundle did not enter unique committed custody",
    )
    return fixture, environment, archived_bundle


def assert_committed_startup_custody_blocked(
    fixture: dict[str, pathlib.Path | str],
    label: str,
) -> None:
    before = file_inventory(pathlib.Path(fixture["case"]))
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", f"{label} was accepted")
    assert_true(
        file_inventory(pathlib.Path(fixture["case"])) == before,
        f"{label} recovery refusal changed fixture state",
    )
    assert_true(
        not (fixture_paths(fixture)["environment"] / "finalization" / "finalize-result.json").exists(),
        f"{label} published a finalize result",
    )


def test_startup_consumption_committed_custody_refusals(base: pathlib.Path) -> dict[str, Any]:
    results: dict[str, str] = {}

    duplicate, _, duplicate_archived = make_committed_finalize_recovery_fixture(
        base, "sr-duplicate"
    )
    current_bundle = pathlib.Path(duplicate["startup_consumption_bundle"])
    shutil.copytree(duplicate_archived, current_bundle)
    assert_committed_startup_custody_blocked(duplicate, "postcommit duplicate startup custody")
    results["current_archived_duplicate"] = "refused_nonreplay"

    missing, _, missing_archived = make_committed_finalize_recovery_fixture(
        base, "sr-missing"
    )
    missing_report = missing_archived / startup_contract.REPORT_FILE
    make_fixture_file_writable(missing_report)
    missing_report.unlink()
    assert_committed_startup_custody_blocked(missing, "postcommit missing startup bundle file")
    results["missing_file"] = "refused_nonreplay"

    extra, _, extra_archived = make_committed_finalize_recovery_fixture(base, "sr-extra")
    write_bytes(extra_archived / "unexpected.extra", b"external")
    assert_committed_startup_custody_blocked(extra, "postcommit extra startup bundle file")
    results["extra_file"] = "refused_nonreplay"

    tampered, _, tampered_archived = make_committed_finalize_recovery_fixture(
        base, "sr-tamper"
    )
    report_path = tampered_archived / startup_contract.REPORT_FILE
    make_fixture_file_writable(report_path)
    write_bytes(report_path, report_path.read_bytes() + b" ")
    assert_committed_startup_custody_blocked(tampered, "postcommit startup bundle byte tamper")
    results["byte_tamper"] = "refused_nonreplay"

    swapped, swapped_environment, swapped_archived = make_committed_finalize_recovery_fixture(
        base, "sr-swap"
    )
    swapped_report = swapped_archived / startup_contract.REPORT_FILE
    make_fixture_file_writable(swapped_report)
    moved_report = swapped_environment / "owned-startup-report.json"
    os.replace(swapped_report, moved_report)
    shutil.copy2(moved_report, swapped_report)
    assert_committed_startup_custody_blocked(swapped, "postcommit same-content inode swap")
    results["same_content_inode_swap"] = "refused_nonreplay"

    hardlink, hardlink_environment, hardlink_archived = make_committed_finalize_recovery_fixture(
        base, "sr-hardlink"
    )
    os.link(
        hardlink_archived / startup_contract.REPORT_FILE,
        hardlink_environment / "startup-report-hardlink-alias.json",
    )
    assert_committed_startup_custody_blocked(hardlink, "postcommit hardlinked startup file")
    results["hardlink"] = "refused_nonreplay"

    reparse, reparse_environment, reparse_archived = make_committed_finalize_recovery_fixture(
        base, "sr-reparse"
    )
    reparse_target = reparse_environment / "startup-bundle-reparse-target"
    os.replace(reparse_archived, reparse_target)
    create_directory_junction(reparse_archived, reparse_target)
    assert_committed_startup_custody_blocked(reparse, "postcommit reparse startup bundle")
    results["reparse"] = "refused_nonreplay"

    shutdown_missing, shutdown_missing_environment, _ = make_committed_finalize_recovery_fixture(
        base, "sr-shutdown-missing"
    )
    missing_shutdown_path = shutdown_missing_environment / "finalization" / "pair" / "shutdown.json"
    make_fixture_file_writable(missing_shutdown_path)
    missing_shutdown_path.unlink()
    assert_committed_startup_custody_blocked(shutdown_missing, "postcommit missing frozen shutdown")
    results["frozen_shutdown_missing"] = "refused_nonreplay"

    shutdown_tamper, shutdown_tamper_environment, _ = make_committed_finalize_recovery_fixture(
        base, "sr-shutdown-tamper"
    )
    shutdown_path = shutdown_tamper_environment / "finalization" / "pair" / "shutdown.json"
    make_fixture_file_writable(shutdown_path)
    write_bytes(shutdown_path, shutdown_path.read_bytes() + b" ")
    assert_committed_startup_custody_blocked(shutdown_tamper, "postcommit frozen shutdown raw tamper")
    results["frozen_shutdown_tamper"] = "refused_nonreplay"

    for duplicate_index, key in enumerate(("origin_archive", "final_event_log")):
        duplicate_log, duplicate_environment, _ = make_committed_finalize_recovery_fixture(
            base,
            f"sr-ld-{duplicate_index}",
        )
        binding = json.loads(
            (duplicate_environment / "runtime-binding.json").read_text(encoding="utf-8")
        )
        relative = pathlib.Path(binding["shutdown_runtime_custody"][key]["relative"])
        archived_log = duplicate_environment / "rb" / relative
        logical_duplicate = pathlib.Path(duplicate_log["oni"]) / "ai-bridge" / relative
        write_bytes(logical_duplicate, archived_log.read_bytes())
        assert_committed_startup_custody_blocked(
            duplicate_log,
            f"postcommit logical duplicate {key}",
        )
        results[f"postcommit_logical_duplicate_{key}"] = "refused_nonreplay"
    return results


def ready_runtime_fixture(
    base: pathlib.Path,
    name: str,
    *,
    run_id: str | None = None,
    with_old_probe: bool = True,
) -> tuple[dict[str, pathlib.Path | str], dict[str, pathlib.Path]]:
    fixture = make_fixture(
        base,
        name,
        run_id=run_id or f"g0-{name}",
        with_old_probe=with_old_probe,
    )
    prepare_fixture(fixture)
    runtime = simulate_runtime(fixture)
    return fixture, runtime


def test_live_mode_fault_flag_preflight_refusals(base: pathlib.Path) -> dict[str, Any]:
    fixture, _ = ready_runtime_fixture(
        base,
        "live-fault-preflight",
        run_id="g0-live-fault-preflight",
    )
    paths = fixture_paths(fixture)
    case = pathlib.Path(fixture["case"])
    oni = pathlib.Path(fixture["oni"])
    evidence = pathlib.Path(fixture["evidence"])
    environment = paths["environment"]
    finalization = environment / "finalization"
    journal = environment / "restore-journal.jsonl"

    def guard_state() -> dict[str, Any]:
        return {
            "case": workflow.snapshot_tree(case),
            "source": workflow.file_state(pathlib.Path(fixture["source"])),
            "source_preview": workflow.file_state(pathlib.Path(fixture["source_preview"])),
            "configs": {
                relative: workflow.optional_file_state(oni / pathlib.Path(relative))
                for relative in workflow.CONFIG_RELATIVES
            },
            "bridge": workflow.snapshot_tree(oni / "ai-bridge"),
            "evidence": workflow.snapshot_tree(evidence),
            "environment": workflow.snapshot_tree(environment),
            "finalization": workflow.snapshot_tree(finalization),
            "journal": workflow.optional_file_state(journal),
        }

    fixture_common = common_args(fixture)
    live_common: list[str] = []
    index = 0
    while index < len(fixture_common):
        if fixture_common[index] in {"--test-root", "--process-state-fixture"}:
            index += 2
            continue
        live_common.append(fixture_common[index])
        index += 1

    base_command = [
        sys.executable,
        "-B",
        str(CORE),
        "finalize",
        *live_common,
        *finalize_binding_args(fixture),
    ]
    fault_options = {
        "test_fail_after_step": ["--test-fail-after-step", "16"],
        "test_finalize_result_fail_after_verify": [
            "--test-finalize-result-fail-after-verify"
        ],
    }
    non_fault_test_anchors = {
        "test_managed_assembly_csharp_sha256",
        "test_managed_assembly_csharp_firstpass_sha256",
    }
    assert_true(
        set(fault_options)
        == set(workflow.FINALIZE_TEST_ONLY_OPTIONS) - non_fault_test_anchors,
        "live preflight matrix does not cover every TestRoot-only finalize option",
    )

    matrix: dict[str, str] = {}
    for option, option_arguments in sorted(fault_options.items()):
        for mode in ("what_if", "apply"):
            before = guard_state()
            mode_argument = "--what-if" if mode == "what_if" else "--apply"
            completed = run(
                [*base_command, *option_arguments, mode_argument],
                expected={2},
                timeout=120,
            )
            blocked = json.loads(completed.stdout)
            assert_true(blocked.get("status") == "blocked", f"live {option} {mode} was not blocked")
            error = str(blocked.get("error") or "")
            assert_true(
                "TestRoot-only finalize options are forbidden in live mode" in error,
                f"live {option} {mode} did not fail at the centralized preflight guard",
            )
            assert_true(
                guard_state() == before,
                f"live {option} {mode} preflight refusal mutated source/config/Bridge/evidence state",
            )
            assert_true(
                not (environment / ".finalize.lock").exists(),
                f"live {option} {mode} created a finalize lock",
            )
            assert_true(
                not (finalization / "finalize-result.json").exists()
                and not (finalization / "finalize-recovery.json").exists(),
                f"live {option} {mode} created finalize result or recovery evidence",
            )
            matrix[f"{option}:{mode}"] = "preflight_blocked_without_mutation"

    abort_base_command = [
        sys.executable,
        "-B",
        str(CORE),
        "abort",
        *live_common,
        *abort_binding_args(fixture),
    ]
    abort_options = {
        "test_abort_fail_after_step": ["--test-abort-fail-after-step", "1"],
        "test_abort_result_fail_after_verify": ["--test-abort-result-fail-after-verify"],
        "test_abort_journal_fail_after_commit": ["--test-abort-journal-fail-after-commit"],
        "test_managed_assembly_csharp_sha256": [
            "--test-managed-assembly-csharp-sha256",
            "sha256:" + "1" * 64,
        ],
        "test_managed_assembly_csharp_firstpass_sha256": [
            "--test-managed-assembly-csharp-firstpass-sha256",
            "sha256:" + "2" * 64,
        ],
    }
    assert_true(
        set(abort_options) == set(workflow.ABORT_TEST_ONLY_OPTIONS),
        "live abort matrix does not cover every TestRoot-only option",
    )
    abort_matrix: dict[str, str] = {}
    for option, option_arguments in sorted(abort_options.items()):
        for mode in ("what_if", "apply"):
            before = guard_state()
            mode_argument = "--what-if" if mode == "what_if" else "--apply"
            completed = run(
                [*abort_base_command, *option_arguments, mode_argument],
                expected={2},
                timeout=120,
            )
            blocked = json.loads(completed.stdout)
            assert_true(blocked.get("status") == "blocked", f"live abort {option} {mode} was not blocked")
            assert_true(
                "TestRoot-only abort options are forbidden in live mode"
                in str(blocked.get("error") or ""),
                f"live abort {option} {mode} did not fail at the centralized preflight guard",
            )
            assert_true(guard_state() == before, f"live abort {option} {mode} changed fixture state")
            assert_true(not (environment / ".finalize.lock").exists(), "live abort guard created a lock")
            assert_true(
                not (environment / "abort" / "preflight.json").exists()
                and not (environment / "abort" / "abort-result.json").exists()
                and not (environment / "abort" / "abort-recovery.json").exists(),
                "live abort guard created evidence",
            )
            abort_matrix[f"{option}:{mode}"] = "preflight_blocked_without_mutation"
    return {
        "guarded_options": sorted(fault_options),
        "modes": ["apply", "what_if"],
        "matrix": matrix,
        "abort_guarded_options": sorted(abort_options),
        "abort_matrix": abort_matrix,
        "source_config_bridge_evidence_inventory": "exactly_unchanged",
        "finalize_lock_created": False,
        "result_or_recovery_created": False,
    }


def rewrite_runtime_config_evidence(
    fixture: dict[str, pathlib.Path | str],
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    path = pathlib.Path(fixture["runtime_config_evidence_path"])
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    unsigned = dict(document)
    unsigned.pop("document_hash", None)
    document["document_hash"] = workflow.sha256_bytes(workflow.canonical_bytes(unsigned))
    write_json(path, document)
    fixture["runtime_config_evidence_sha256"] = sha256(path)
    return document


def assert_runtime_config_finalize_blocked(
    fixture: dict[str, pathlib.Path | str],
    label: str,
) -> str:
    before = file_inventory(pathlib.Path(fixture["case"]))
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", f"{label} was accepted")
    assert_true(
        file_inventory(pathlib.Path(fixture["case"])) == before,
        f"{label} refusal mutated the TestRoot fixture",
    )
    return str(blocked.get("error") or "")


def publish_resealed_runtime_bootstrap_result(
    fixture: dict[str, pathlib.Path | str],
    result: dict[str, Any],
    *,
    mutate_evidence: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Reseal every outer exact-record/startup anchor around a forged result."""

    result_path = pathlib.Path(fixture["runtime_bootstrap_result"])
    unsigned_result = dict(result)
    unsigned_result.pop("result_hash", None)
    result["result_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(unsigned_result)
    )
    result_payload = runtime_config_contract.json_document_bytes(result)
    write_bytes(result_path, result_payload)
    result_raw_sha256 = workflow.sha256_bytes(result_payload)

    evidence_path = pathlib.Path(fixture["runtime_config_evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    record = evidence["runtime"]["runtime_bootstrap_result"]
    record.update(
        {
            "external_expected_sha256": result_raw_sha256,
            "raw_sha256": result_raw_sha256,
            "raw_byte_length": len(result_payload),
            "raw_utf8_base64": base64.b64encode(result_payload).decode("ascii"),
            "result_hash": result["result_hash"],
            "document": copy.deepcopy(result),
        }
    )
    if mutate_evidence is not None:
        mutate_evidence(evidence)
    intent = evidence["startup_intent"]
    intent["runtime_bootstrap_result_sha256"] = result_raw_sha256
    intent["runtime_bootstrap_result_hash"] = result["result_hash"]
    unsigned_intent = dict(intent)
    unsigned_intent.pop("document_hash", None)
    intent["document_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(unsigned_intent)
    )
    unsigned_evidence = dict(evidence)
    unsigned_evidence.pop("document_hash", None)
    evidence["document_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(unsigned_evidence)
    )
    write_json(evidence_path, evidence)
    evidence_raw_sha256 = sha256(evidence_path)

    startup_binding_path = pathlib.Path(fixture["startup_binding_path"])
    startup_binding = json.loads(startup_binding_path.read_text(encoding="utf-8"))
    startup_binding["runtime_config_evidence_sha256"] = evidence_raw_sha256
    startup_binding["startup_intent_hash"] = intent["document_hash"]
    unsigned_binding = dict(startup_binding)
    unsigned_binding.pop("document_hash", None)
    startup_binding["document_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(unsigned_binding)
    )
    write_json(startup_binding_path, startup_binding)

    fixture["expected_runtime_bootstrap_result_sha256"] = result_raw_sha256
    fixture["runtime_config_evidence_sha256"] = evidence_raw_sha256
    fixture["startup_binding_sha256"] = sha256(startup_binding_path)


def rewrite_runtime_bootstrap_recovery_projection(
    fixture: dict[str, pathlib.Path | str],
    recovery_relative: str | None,
) -> None:
    """Coherently reseal a forged old-Probe recovery projection."""

    result_path = pathlib.Path(fixture["runtime_bootstrap_result"])
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["old_probe_recovery_relative"] = recovery_relative
    commit_entry = result["install_commit_entry"]
    commit_entry["data"]["old_probe_recovery"] = recovery_relative
    result["install_commit_entry_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(commit_entry)
    )
    publish_resealed_runtime_bootstrap_result(fixture, result)


def test_runtime_bootstrap_result_anchor_refusals(
    base: pathlib.Path,
) -> dict[str, Any]:
    results: dict[str, Any] = {}

    missing_result, _ = ready_runtime_fixture(base, "rb-miss")
    pathlib.Path(missing_result["runtime_bootstrap_result"]).unlink()
    assert_runtime_config_finalize_blocked(
        missing_result,
        "missing runtime bootstrap result",
    )
    results["missing_runtime_bootstrap_result"] = "refused_zero_write"

    wrong_result_hash, _ = ready_runtime_fixture(base, "rb-hash")
    wrong_result_hash["expected_runtime_bootstrap_result_sha256"] = (
        "sha256:" + "0" * 64
    )
    assert_runtime_config_finalize_blocked(
        wrong_result_hash,
        "wrong external runtime bootstrap result SHA-256",
    )
    results["wrong_runtime_bootstrap_result_sha256"] = "refused_zero_write"

    wrong_result_path, _ = ready_runtime_fixture(base, "rb-path")
    canonical_result_path = pathlib.Path(
        wrong_result_path["runtime_bootstrap_result"]
    )
    alternate_result_path = canonical_result_path.with_name("copied-result.json")
    write_bytes(alternate_result_path, canonical_result_path.read_bytes())
    wrong_result_path["runtime_bootstrap_result"] = alternate_result_path
    assert_runtime_config_finalize_blocked(
        wrong_result_path,
        "noncanonical runtime bootstrap result path",
    )
    results["wrong_runtime_bootstrap_result_path"] = "refused_zero_write"

    rehashed_result, _ = ready_runtime_fixture(base, "rb-rehash")
    rehashed_result_path = pathlib.Path(rehashed_result["runtime_bootstrap_result"])
    rehashed_result_document = json.loads(
        rehashed_result_path.read_text(encoding="utf-8")
    )
    rehashed_result_document["classification"] = "coherently_rehashed_fixture"
    unsigned_rehashed_result = dict(rehashed_result_document)
    unsigned_rehashed_result.pop("result_hash", None)
    rehashed_result_document["result_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(unsigned_rehashed_result)
    )
    write_json(rehashed_result_path, rehashed_result_document)
    rehashed_result["expected_runtime_bootstrap_result_sha256"] = sha256(
        rehashed_result_path
    )
    assert_runtime_config_finalize_blocked(
        rehashed_result,
        "self-rehashed runtime bootstrap result with synchronized CLI anchor",
    )
    results["rehashed_runtime_bootstrap_result"] = "frozen_record_refused"

    probe_tree_mismatch, _ = ready_runtime_fixture(
        base,
        "rb-tree",
    )
    rewrite_runtime_config_evidence(
        probe_tree_mismatch,
        lambda document: document["runtime"]["installed_probe"].__setitem__(
            "tree_content_hash",
            "sha256:" + "7" * 64,
        ),
    )
    probe_tree_error = assert_runtime_config_finalize_blocked(
        probe_tree_mismatch,
        "runtime bootstrap result/installed Probe tree mismatch",
    )
    assert_true(
        "exact installed run" in probe_tree_error,
        "Probe-tree mismatch did not reach the strict frozen result cross-binding",
    )
    results["runtime_bootstrap_probe_tree_mismatch"] = "refused_explicitly"

    anchor_override, _ = ready_runtime_fixture(
        base,
        "rb-override",
    )
    forged_anchor = workflow.sha256_bytes(b"forged-runtime-bootstrap-anchor")
    rewrite_runtime_config_evidence(
        anchor_override,
        lambda document: document["runtime"]["runtime_bootstrap_result"].__setitem__(
            "external_expected_sha256",
            forged_anchor,
        ),
    )
    anchor_override["expected_runtime_bootstrap_result_sha256"] = forged_anchor
    assert_runtime_config_finalize_blocked(
        anchor_override,
        "embedded runtime-result anchor overriding the external file anchor",
    )
    results["runtime_bootstrap_anchor_override"] = (
        "external_file_anchor_remained_authoritative"
    )

    return results


def test_optional_old_probe_recovery_contract(base: pathlib.Path) -> dict[str, Any]:
    """Exercise the exact present/absent old-Probe recovery state machine."""

    finalize_fixture = make_fixture(
        base,
        "rb-old-probe-absent-finalize",
        run_id="g0-rb-old-probe-absent-fin",
        with_old_probe=False,
    )
    finalize_probe = (
        pathlib.Path(finalize_fixture["oni"])
        / "mods"
        / "Dev"
        / "oni-ai-colony-agent"
    )
    assert_true(not finalize_probe.exists(), "absent-old-Probe fixture created Probe")
    prepare_fixture(finalize_fixture)
    simulate_runtime(finalize_fixture)
    finalize_runtime_result = json.loads(
        pathlib.Path(finalize_fixture["runtime_bootstrap_result"]).read_text(
            encoding="utf-8"
        )
    )
    finalize_recovery_root = (
        pathlib.Path(finalize_fixture["runtime_bootstrap_result"]).parent
        / "recovery"
    )
    assert_true(
        finalize_runtime_result.get("old_probe_recovery_relative") is None
        and finalize_recovery_root.is_dir()
        and not any(finalize_recovery_root.iterdir()),
        "authentic absent-old-Probe bootstrap recovery projection/tree is invalid",
    )
    finalize_whatif_before = file_inventory(pathlib.Path(finalize_fixture["case"]))
    finalize_plan = json.loads(core(finalize_fixture, "finalize", what_if=True).stdout)
    assert_true(
        finalize_plan.get("status") == "would_finalize"
        and file_inventory(pathlib.Path(finalize_fixture["case"]))
        == finalize_whatif_before,
        "absent-old-Probe finalize WhatIf was not exactly read-only",
    )
    finalize_result = json.loads(core(finalize_fixture, "finalize", apply=True).stdout)
    assert_true(
        finalize_result.get("status") == "finalized" and not finalize_probe.exists(),
        "absent-old-Probe finalize did not restore the exact absent state",
    )

    abort_fixture = make_fixture(
        base,
        "rb-old-probe-absent-abort",
        run_id="g0-rb-old-probe-absent-abort",
        with_old_probe=False,
    )
    abort_probe = (
        pathlib.Path(abort_fixture["oni"])
        / "mods"
        / "Dev"
        / "oni-ai-colony-agent"
    )
    abort_baseline = file_inventory(pathlib.Path(abort_fixture["oni"]))
    prepare_fixture(abort_fixture)
    prepare_configured_runtime(abort_fixture)
    abort_runtime_result = json.loads(
        pathlib.Path(abort_fixture["runtime_bootstrap_result"]).read_text(
            encoding="utf-8"
        )
    )
    abort_whatif_before = file_inventory(pathlib.Path(abort_fixture["case"]))
    abort_plan = json.loads(core(abort_fixture, "abort", what_if=True).stdout)
    assert_true(
        abort_runtime_result.get("old_probe_recovery_relative") is None
        and abort_plan.get("status") == "would_abort"
        and file_inventory(pathlib.Path(abort_fixture["case"]))
        == abort_whatif_before,
        "absent-old-Probe abort WhatIf or result projection is invalid",
    )
    abort_result = json.loads(core(abort_fixture, "abort", apply=True).stdout)
    assert_true(
        abort_result.get("status") == "aborted"
        and not abort_probe.exists()
        and file_inventory(pathlib.Path(abort_fixture["oni"])) == abort_baseline,
        "absent-old-Probe abort did not restore the exact absent state",
    )

    forged_none, _ = ready_runtime_fixture(
        base,
        "rb-old-probe-forged-none",
        run_id="g0-rb-old-probe-forged-none",
    )
    rewrite_runtime_bootstrap_recovery_projection(forged_none, None)
    forged_none_error = assert_runtime_config_finalize_blocked(
        forged_none,
        "present old-Probe with coherently resealed None recovery projection",
    )
    assert_true(
        "runtime bootstrap" in forged_none_error
        and (
            "old-Probe" in forged_none_error
            or "desired document set" in forged_none_error
        ),
        "forged None did not reach the old-Probe prestate cross-binding",
    )

    forged_path, _ = ready_runtime_fixture(
        base,
        "rb-old-probe-forged-path",
        run_id="g0-rb-old-probe-forged-path",
        with_old_probe=False,
    )
    result_path = pathlib.Path(forged_path["runtime_bootstrap_result"])
    evidence_path = pathlib.Path(forged_path["runtime_config_evidence_path"])
    startup_binding_path = pathlib.Path(forged_path["startup_binding_path"])
    original_publications = {
        result_path: result_path.read_bytes(),
        evidence_path: evidence_path.read_bytes(),
        startup_binding_path: startup_binding_path.read_bytes(),
    }
    original_hashes = {
        "expected_runtime_bootstrap_result_sha256": forged_path[
            "expected_runtime_bootstrap_result_sha256"
        ],
        "runtime_config_evidence_sha256": forged_path[
            "runtime_config_evidence_sha256"
        ],
        "startup_binding_sha256": forged_path["startup_binding_sha256"],
    }
    rewrite_runtime_bootstrap_recovery_projection(
        forged_path,
        "recovery/old-probe",
    )
    forged_path_error = assert_runtime_config_finalize_blocked(
        forged_path,
        "absent old-Probe with coherently resealed recovery path",
    )
    assert_true(
        "runtime bootstrap" in forged_path_error
        and (
            "old-Probe" in forged_path_error
            or "desired document set" in forged_path_error
        ),
        "forged recovery path did not reach the old-Probe prestate cross-binding",
    )
    for path, payload in original_publications.items():
        write_bytes(path, payload)
    forged_path.update(original_hashes)
    extra_recovery = result_path.parent / "recovery" / "old-probe" / "foreign.bin"
    write_bytes(extra_recovery, b"externally-injected-recovery-tree")
    extra_error = assert_runtime_config_finalize_blocked(
        forged_path,
        "absent old-Probe with an unexpected recovery tree",
    )
    assert_true(
        "unexpected recovery tree" in extra_error and extra_recovery.is_file(),
        "extra recovery tree refusal was not explicit/non-destructive",
    )
    return {
        "authentic_absent_finalize": "whatif_zero_write_apply_restored_absent",
        "authentic_absent_abort": "whatif_zero_write_apply_restored_absent",
        "forged_none_present_prestate": "refused_zero_write",
        "forged_path_absent_prestate": "refused_zero_write",
        "extra_recovery_tree_absent_prestate": "refused_zero_write_retained",
    }


def test_offline_runtime_bootstrap_semantic_refusals(
    base: pathlib.Path,
) -> dict[str, Any]:
    """Reject coherently resealed deep-result drift on finalized replay paths."""

    fixture = make_fixture(
        base,
        "rb-offline-semantic-refusals",
        run_id="g0-rb-offline-semantic",
    )
    prepare_fixture(fixture)
    simulate_runtime(fixture)
    committed = json.loads(core(fixture, "finalize", apply=True).stdout)
    assert_true(committed.get("status") == "finalized", "offline refusal fixture did not finalize")
    environment = fixture_paths(fixture)["environment"]
    archived_startup = (
        environment / "rb" / runtime_config_contract.STARTUP_BINDING_NAME
    )
    assert_true(
        archived_startup.is_file(),
        "finalized offline refusal fixture lacks canonical startup-binding custody",
    )
    fixture["startup_binding_path"] = archived_startup
    result_path = pathlib.Path(fixture["runtime_bootstrap_result"])
    evidence_path = pathlib.Path(fixture["runtime_config_evidence_path"])
    startup_binding_path = archived_startup
    finalize_result_path = environment / "finalization" / "finalize-result.json"
    finalize_result_sha256 = sha256(finalize_result_path)
    originals = {
        result_path: result_path.read_bytes(),
        evidence_path: evidence_path.read_bytes(),
        startup_binding_path: startup_binding_path.read_bytes(),
    }
    original_evidence_document = json.loads(originals[evidence_path].decode("utf-8"))
    original_hashes = {
        "expected_runtime_bootstrap_result_sha256": fixture[
            "expected_runtime_bootstrap_result_sha256"
        ],
        "runtime_config_evidence_sha256": fixture["runtime_config_evidence_sha256"],
        "startup_binding_sha256": fixture["startup_binding_sha256"],
    }

    def restore_publications() -> None:
        for path, payload in originals.items():
            write_bytes(path, payload)
        fixture.update(original_hashes)

    def mutate_nested_lock(result: dict[str, Any]) -> None:
        claim = result["install_lock_claim"]
        claim["purpose"] = "coherently-rehashed-foreign-purpose"
        unsigned_claim = dict(claim)
        unsigned_claim.pop("claim_hash", None)
        claim["claim_hash"] = workflow.sha256_bytes(
            workflow.canonical_bytes(unsigned_claim)
        )
        result["install_lock_claim_hash"] = claim["claim_hash"]

    def mutate_candidate(result: dict[str, Any]) -> None:
        forged_candidate = {
            "schema": "oni.live-runtime-bootstrap-result-candidate.v1",
            "classification": "coherently_rehashed_foreign_candidate",
        }
        result["candidate_hash"] = workflow.sha256_bytes(
            workflow.canonical_bytes(forged_candidate)
        )
        result["candidate_sha256"] = workflow.sha256_bytes(
            runtime_config_contract.json_document_bytes(forged_candidate)
        )

    def reseal_bootstrap_record(
        evidence: dict[str, Any],
        forged_bootstrap: dict[str, Any],
    ) -> None:
        payload = runtime_config_contract.json_document_bytes(forged_bootstrap)
        record = evidence["runtime"]["bootstrap"]
        record.update(
            {
                "raw_sha256": workflow.sha256_bytes(payload),
                "raw_byte_length": len(payload),
                "raw_utf8_base64": base64.b64encode(payload).decode("ascii"),
                "document_hash": workflow.sha256_bytes(
                    workflow.canonical_bytes(forged_bootstrap)
                ),
                "document": copy.deepcopy(forged_bootstrap),
            }
        )

    def forged_bootstrap_with_timestamp() -> dict[str, Any]:
        document = copy.deepcopy(
            original_evidence_document["runtime"]["bootstrap"]["document"]
        )
        document["prepared_at"] = "2026-09-01T00:00:00.000000+00:00"
        return document

    def mutate_bootstrap(result: dict[str, Any]) -> None:
        forged_bootstrap = forged_bootstrap_with_timestamp()
        result["bootstrap_document_hash"] = workflow.sha256_bytes(
            workflow.canonical_bytes(forged_bootstrap)
        )
        result["bootstrap_file_sha256"] = workflow.sha256_bytes(
            runtime_config_contract.json_document_bytes(forged_bootstrap)
        )

    def mutate_bootstrap_evidence(evidence: dict[str, Any]) -> None:
        reseal_bootstrap_record(evidence, forged_bootstrap_with_timestamp())

    def mutate_archive(result: dict[str, Any]) -> None:
        forged_relative = (
            f"ai-bridge-archives/{fixture['run_id']}-coherently-rehashed-foreign"
        )
        result["archive_relative"] = forged_relative
        commit_entry = result["install_commit_entry"]
        commit_entry["data"]["archive_relative"] = forged_relative
        result["install_commit_entry_hash"] = workflow.sha256_bytes(
            workflow.canonical_bytes(commit_entry)
        )
        forged_bootstrap = copy.deepcopy(
            original_evidence_document["runtime"]["bootstrap"]["document"]
        )
        forged_bootstrap["archived_previous_bridge"] = str(
            pathlib.Path(fixture["oni"]) / pathlib.Path(forged_relative)
        )
        result["bootstrap_document_hash"] = workflow.sha256_bytes(
            workflow.canonical_bytes(forged_bootstrap)
        )
        result["bootstrap_file_sha256"] = workflow.sha256_bytes(
            runtime_config_contract.json_document_bytes(forged_bootstrap)
        )

    def mutate_archive_evidence(evidence: dict[str, Any]) -> None:
        forged_relative = (
            f"ai-bridge-archives/{fixture['run_id']}-coherently-rehashed-foreign"
        )
        forged_bootstrap = copy.deepcopy(
            original_evidence_document["runtime"]["bootstrap"]["document"]
        )
        forged_bootstrap["archived_previous_bridge"] = str(
            pathlib.Path(fixture["oni"]) / pathlib.Path(forged_relative)
        )
        reseal_bootstrap_record(evidence, forged_bootstrap)

    def mutate_producer(result: dict[str, Any]) -> None:
        forged_payload = b"coherently-rehashed-foreign-producer"
        result["producer"] = {
            "length": len(forged_payload),
            "sha256": workflow.sha256_bytes(forged_payload),
        }

    mutations: list[
        tuple[
            str,
            Callable[[dict[str, Any]], None],
            Callable[[dict[str, Any]], None] | None,
        ]
    ] = [
        ("nested_lock", mutate_nested_lock, None),
        ("candidate", mutate_candidate, None),
        ("bootstrap", mutate_bootstrap, mutate_bootstrap_evidence),
        ("archive", mutate_archive, mutate_archive_evidence),
        ("producer", mutate_producer, None),
    ]
    results: dict[str, str] = {}
    try:
        for label, mutate, mutate_evidence in mutations:
            restore_publications()
            result = json.loads(originals[result_path].decode("utf-8"))
            mutate(result)
            publish_resealed_runtime_bootstrap_result(
                fixture,
                result,
                mutate_evidence=mutate_evidence,
            )
            error = assert_runtime_config_finalize_blocked(
                fixture,
                f"offline coherently resealed runtime bootstrap {label}",
            )
            journal = workflow.verify_journal(
                environment / "restore-journal.jsonl"
            )
            assert_true(
                "runtime bootstrap" in error
                and sha256(finalize_result_path) == finalize_result_sha256
                and sum(
                    entry.get("event") == "restore_transaction_physical_committed"
                    for entry in journal
                )
                == 1,
                f"offline {label} refusal replayed or bypassed semantic validation",
            )
            results[label] = "refused_zero_write_nonreplay"
    finally:
        restore_publications()
    return results


def test_runtime_config_external_anchor_refusals(base: pathlib.Path) -> dict[str, Any]:
    results: dict[str, Any] = {}

    missing, _ = ready_runtime_fixture(base, "config-missing")
    pathlib.Path(missing["runtime_config_evidence_path"]).unlink()
    assert_runtime_config_finalize_blocked(missing, "missing runtime config evidence")
    results["missing_evidence"] = "refused"

    wrong_path, _ = ready_runtime_fixture(base, "config-wrong-path")
    moved = fixture_paths(wrong_path)["environment"] / "runtime-config-evidence-moved.json"
    os.replace(pathlib.Path(wrong_path["runtime_config_evidence_path"]), moved)
    wrong_path["runtime_config_evidence_path"] = moved
    assert_runtime_config_finalize_blocked(wrong_path, "non-unique runtime config evidence path")
    results["wrong_exact_path"] = "refused"

    wrong_sha, _ = ready_runtime_fixture(base, "config-wrong-sha")
    wrong_sha["runtime_config_evidence_sha256"] = "sha256:" + "0" * 64
    assert_runtime_config_finalize_blocked(wrong_sha, "wrong external runtime config SHA-256")
    results["wrong_external_evidence_sha256"] = "refused"

    wrong_startup, _ = ready_runtime_fixture(base, "config-wrong-startup-sha")
    wrong_startup["startup_binding_sha256"] = "sha256:" + "0" * 64
    assert_runtime_config_finalize_blocked(
        wrong_startup, "wrong external startup binding SHA-256"
    )
    results["wrong_external_startup_binding_sha256"] = "refused"

    missing_startup, _ = ready_runtime_fixture(base, "config-missing-startup")
    pathlib.Path(missing_startup["startup_binding_path"]).unlink()
    assert_runtime_config_finalize_blocked(missing_startup, "missing startup binding")
    results["missing_startup_binding"] = "refused"

    startup_hardlink, _ = ready_runtime_fixture(base, "config-startup-hardlink")
    startup_path = pathlib.Path(startup_hardlink["startup_binding_path"])
    os.link(startup_path, startup_path.with_name("runtime-startup-binding-alias.json"))
    assert_runtime_config_finalize_blocked(startup_hardlink, "hardlinked startup binding")
    results["startup_binding_hardlink"] = "refused"

    startup_tamper, _ = ready_runtime_fixture(base, "config-startup-rehash")
    startup_path = pathlib.Path(startup_tamper["startup_binding_path"])
    startup_document = json.loads(startup_path.read_text(encoding="utf-8"))
    startup_document["expected_game_build"] = "cl-000000"
    unsigned_startup = dict(startup_document)
    unsigned_startup.pop("document_hash", None)
    startup_document["document_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(unsigned_startup)
    )
    write_json(startup_path, startup_document)
    startup_tamper["startup_binding_sha256"] = sha256(startup_path)
    assert_runtime_config_finalize_blocked(
        startup_tamper, "self-rehashed startup binding semantic tamper"
    )
    results["startup_binding_rehashed_crosslink_tamper"] = "refused"

    duplicate_startup, _ = ready_runtime_fixture(base, "config-startup-duplicate")
    duplicate_path = (
        fixture_paths(duplicate_startup)["environment"]
        / "abort"
        / "rb"
        / runtime_config_contract.STARTUP_BINDING_NAME
    )
    write_bytes(
        duplicate_path,
        pathlib.Path(duplicate_startup["startup_binding_path"]).read_bytes(),
    )
    assert_runtime_config_finalize_blocked(
        duplicate_startup, "ambiguous duplicate startup binding custody"
    )
    results["startup_binding_duplicate_custody"] = "refused"

    rehashed, _ = ready_runtime_fixture(base, "config-rehashed-tamper")
    rewrite_runtime_config_evidence(
        rehashed,
        lambda document: document["runtime"].__setitem__("mod_version", "0.7.1"),
    )
    assert_runtime_config_finalize_blocked(rehashed, "self-rehashed runtime config semantic tamper")
    results["tamper_internal_hash_and_cli_anchor_synced"] = "cross_binding_refused"

    for name, mutation in (
        ("wrong-run", lambda document: document.__setitem__("run_id", "different-run")),
        (
            "wrong-manifest",
            lambda document: document.__setitem__(
                "environment_manifest_sha256", "sha256:" + "1" * 64
            ),
        ),
        (
            "wrong-source",
            lambda document: document["bindings"]["source_save"].__setitem__(
                "path_id_lower", "sha256:" + "2" * 64
            ),
        ),
        (
            "wrong-loader",
            lambda document: document["bindings"]["loader_save"].__setitem__(
                "path_id_lower", "sha256:" + "3" * 64
            ),
        ),
    ):
        binding_fixture, _ = ready_runtime_fixture(base, f"config-{name}")
        rewrite_runtime_config_evidence(binding_fixture, mutation)
        assert_runtime_config_finalize_blocked(binding_fixture, f"runtime config {name} binding")
        results[name.replace("-", "_")] = "refused"

    recovery, _ = ready_runtime_fixture(base, "config-recovery-latch")
    write_json(
        fixture_paths(recovery)["environment"] / "runtime-config-recovery-fixture.json",
        {"schema": "oni.runtime-config-recovery.fixture.v1"},
    )
    assert_runtime_config_finalize_blocked(recovery, "runtime config recovery latch")
    results["runtime_config_recovery_record"] = "hard_blocked"

    hardlink, _ = ready_runtime_fixture(base, "config-evidence-hardlink")
    evidence_path = pathlib.Path(hardlink["runtime_config_evidence_path"])
    os.link(evidence_path, evidence_path.with_name("runtime-config-evidence-alias.json"))
    assert_runtime_config_finalize_blocked(hardlink, "hardlinked runtime config evidence")
    results["runtime_config_evidence_hardlink"] = "refused"

    late, _ = ready_runtime_fixture(base, "config-late")
    shutdown = json.loads(pathlib.Path(late["shutdown_evidence_path"]).read_text(encoding="utf-8"))
    session_start = datetime.fromisoformat(
        str(shutdown["termination"]["session_started"]["ts"]).replace("Z", "+00:00")
    )
    late_timestamp = (session_start + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    )
    rewrite_runtime_config_evidence(
        late,
        lambda document: document.__setitem__(
            "configured_at", late_timestamp
        ),
    )
    late_binding_path = pathlib.Path(late["startup_binding_path"])
    late_binding = json.loads(late_binding_path.read_text(encoding="utf-8"))
    late_binding["runtime_config_evidence_sha256"] = late[
        "runtime_config_evidence_sha256"
    ]
    late_binding["created_at"] = late_timestamp
    late_binding_unsigned = dict(late_binding)
    late_binding_unsigned.pop("document_hash", None)
    late_binding["document_hash"] = workflow.sha256_bytes(
        workflow.canonical_bytes(late_binding_unsigned)
    )
    write_json(late_binding_path, late_binding)
    late["startup_binding_sha256"] = sha256(late_binding_path)
    error = assert_runtime_config_finalize_blocked(late, "late runtime configuration")
    assert_true(
        "runtime_config_after_trusted_startup_origin" in error,
        "late ordering refusal was not explicit",
    )
    results["configured_at_after_trusted_startup_origin"] = "refused_explicitly"
    results["configured_at_equal_trusted_startup_origin"] = "covered_by_positive_fixture"

    release_path, _ = ready_runtime_fixture(base, "config-release-path")
    wrong_release = pathlib.Path(release_path["case"]) / "workspace" / "other" / "OniAiProbeMod.dll"
    write_bytes(wrong_release, pathlib.Path(release_path["release_dll"]).read_bytes())
    release_path["release_dll"] = wrong_release
    assert_runtime_config_finalize_blocked(release_path, "wrong exact Release DLL path")
    results["wrong_release_path"] = "refused"

    release_hash, _ = ready_runtime_fixture(base, "config-release-hash")
    release_hash["expected_release_dll_sha256"] = "sha256:" + "4" * 64
    assert_runtime_config_finalize_blocked(release_hash, "wrong external Release DLL SHA-256")
    results["wrong_release_sha256"] = "refused"

    malicious, malicious_runtime = ready_runtime_fixture(base, "config-coherent-malicious-dll")
    malicious_bytes = b"coherent-malicious-probe-rewrite\x00"
    write_bytes(pathlib.Path(malicious["release_dll"]), malicious_bytes)
    write_bytes(
        pathlib.Path(malicious["oni"])
        / "mods"
        / "Dev"
        / "oni-ai-colony-agent"
        / "OniAiProbeMod.dll",
        malicious_bytes,
    )
    malicious_hash = sha256(pathlib.Path(malicious["release_dll"]))
    bootstrap_path = malicious_runtime["bridge"] / "v27-runtime-bootstrap.json"
    bootstrap_document = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    bootstrap_document["built_dll_sha256"] = malicious_hash
    bootstrap_document["installed_dll_sha256"] = malicious_hash
    write_json(bootstrap_path, bootstrap_document)

    def rewrite_coherent(document: dict[str, Any]) -> None:
        bootstrap_raw = bootstrap_path.read_bytes()
        bootstrap_record = document["runtime"]["bootstrap"]
        bootstrap_record["raw_sha256"] = workflow.sha256_bytes(bootstrap_raw)
        bootstrap_record["raw_byte_length"] = len(bootstrap_raw)
        bootstrap_record["raw_utf8_base64"] = base64.b64encode(bootstrap_raw).decode("ascii")
        bootstrap_record["document"] = bootstrap_document
        bootstrap_record["document_hash"] = workflow.sha256_bytes(
            workflow.canonical_bytes(bootstrap_document)
        )
        document["runtime"]["release_dll"]["external_expected_sha256"] = malicious_hash
        document["runtime"]["release_dll"]["file_sha256"] = malicious_hash
        document["runtime"]["installed_probe"]["dll_sha256"] = malicious_hash

    rewrite_runtime_config_evidence(malicious, rewrite_coherent)
    assert_runtime_config_finalize_blocked(
        malicious, "coherent malicious DLL/bootstrap/evidence rewrite"
    )
    results["coherent_malicious_dll_bootstrap_evidence"] = "external_release_anchor_refused"
    return results


def workflow_finalize_namespace(
    fixture: dict[str, pathlib.Path | str],
    *,
    apply: bool,
) -> Any:
    values = ["finalize", *common_args(fixture), *finalize_binding_args(fixture)]
    values.append("--apply" if apply else "--what-if")
    return workflow.build_parser().parse_args(values)


def test_runtime_config_custody_concurrency_refusals(base: pathlib.Path) -> dict[str, Any]:
    results: dict[str, Any] = {}

    swap_fixture, _ = ready_runtime_fixture(base, "config-verify-name-swap")
    swap_path = pathlib.Path(swap_fixture["runtime_config_evidence_path"])
    moved_owned = swap_path.with_name("runtime-config-evidence-owned-moved.json")
    external_bytes = b'{"external_replacement":true}\n'
    original_verify = workflow.runtime_config_core.verify_evidence

    def swap_after_verify(
        namespace: Any,
        *,
        active_parent_finalize_lock: Any = None,
    ) -> dict[str, Any]:
        result = original_verify(
            namespace,
            active_parent_finalize_lock=active_parent_finalize_lock,
        )
        os.replace(swap_path, moved_owned)
        write_bytes(swap_path, external_bytes)
        return result

    before_swap = file_inventory(pathlib.Path(swap_fixture["oni"]))
    workflow.runtime_config_core.verify_evidence = swap_after_verify
    try:
        try:
            workflow.finalize(workflow_finalize_namespace(swap_fixture, apply=False))
            raise VerificationFailure("runtime config post-verify name swap was accepted")
        except workflow.WorkflowError:
            pass
    finally:
        workflow.runtime_config_core.verify_evidence = original_verify
    assert_true(swap_path.read_bytes() == external_bytes, "external evidence replacement was deleted")
    assert_true(moved_owned.exists(), "owned runtime config evidence disappeared after name swap")
    assert_true(
        file_inventory(pathlib.Path(swap_fixture["oni"])) == before_swap,
        "runtime config evidence name-swap refusal mutated ONI",
    )
    results["evidence_after_verify_name_swap"] = "external_replacement_preserved"

    binding_swap, _ = ready_runtime_fixture(base, "cfg-binding-swap")
    binding_path = pathlib.Path(binding_swap["startup_binding_path"])
    binding_owned = binding_path.with_name("runtime-startup-binding-owned-moved.json")
    binding_external = b'{"external_replacement":true}\n'
    original_verify = workflow.runtime_config_core.verify_evidence

    def swap_binding_after_verify(
        namespace: Any,
        *,
        active_parent_finalize_lock: Any = None,
    ) -> dict[str, Any]:
        result = original_verify(
            namespace,
            active_parent_finalize_lock=active_parent_finalize_lock,
        )
        os.replace(binding_path, binding_owned)
        write_bytes(binding_path, binding_external)
        return result

    before_binding_swap = file_inventory(pathlib.Path(binding_swap["oni"]))
    workflow.runtime_config_core.verify_evidence = swap_binding_after_verify
    try:
        try:
            workflow.finalize(workflow_finalize_namespace(binding_swap, apply=False))
            raise VerificationFailure("startup binding post-verify name swap was accepted")
        except workflow.WorkflowError:
            pass
    finally:
        workflow.runtime_config_core.verify_evidence = original_verify
    assert_true(
        binding_path.read_bytes() == binding_external,
        "external startup binding replacement was deleted",
    )
    assert_true(binding_owned.exists(), "owned startup binding disappeared after name swap")
    after_binding_swap = file_inventory(pathlib.Path(binding_swap["oni"]))
    changed_binding_paths = {
        key
        for key in set(before_binding_swap) | set(after_binding_swap)
        if before_binding_swap.get(key) != after_binding_swap.get(key)
    }
    assert_true(
        changed_binding_paths
        == {
            "ai-bridge/runtime-startup-binding.json",
            "ai-bridge/runtime-startup-binding-owned-moved.json",
        },
        "startup binding name-swap refusal mutated unrelated ONI state",
    )
    results["startup_binding_after_verify_name_swap"] = "external_replacement_preserved"

    drift_fixture, _ = ready_runtime_fixture(base, "cfg-inplace-drift")
    drift_path = pathlib.Path(drift_fixture["runtime_config_evidence_path"])
    original_verify = workflow.runtime_config_core.verify_evidence

    def drift_after_verify(
        namespace: Any,
        *,
        active_parent_finalize_lock: Any = None,
    ) -> dict[str, Any]:
        result = original_verify(
            namespace,
            active_parent_finalize_lock=active_parent_finalize_lock,
        )
        with drift_path.open("ab") as handle:
            handle.write(b" ")
        return result

    before_drift = file_inventory(pathlib.Path(drift_fixture["oni"]))
    workflow.runtime_config_core.verify_evidence = drift_after_verify
    try:
        try:
            workflow.finalize(workflow_finalize_namespace(drift_fixture, apply=False))
            raise VerificationFailure("runtime config post-verify in-place drift was accepted")
        except workflow.WorkflowError:
            pass
    finally:
        workflow.runtime_config_core.verify_evidence = original_verify
    assert_true(
        file_inventory(pathlib.Path(drift_fixture["oni"])) == before_drift,
        "runtime config evidence in-place drift refusal mutated ONI",
    )
    results["evidence_after_verify_inplace_drift"] = "refused"

    conflict, _ = ready_runtime_fixture(base, "config-frozen-conflict")
    conflict_target = (
        fixture_paths(conflict)["environment"] / "finalization" / "pair" / "config.json"
    )
    write_bytes(conflict_target, b"external frozen conflict\n")
    assert_runtime_config_finalize_blocked(conflict, "conflicting frozen runtime config copy")
    results["frozen_copy_conflict"] = "refused_unmodified"

    hardlink, _ = ready_runtime_fixture(base, "config-frozen-hardlink")
    hardlink_target = (
        fixture_paths(hardlink)["environment"] / "finalization" / "pair" / "config.json"
    )
    write_bytes(hardlink_target, pathlib.Path(hardlink["runtime_config_evidence_path"]).read_bytes())
    os.link(hardlink_target, hardlink_target.with_name("config-alias.json"))
    assert_runtime_config_finalize_blocked(hardlink, "hardlinked frozen runtime config copy")
    results["frozen_copy_hardlink"] = "refused"

    publication, _ = ready_runtime_fixture(base, "config-frozen-name-swap")
    publication_target = (
        fixture_paths(publication)["environment"] / "finalization" / "pair" / "config.json"
    )
    publication_owned = publication_target.with_name("config-owned-moved.json")
    publication_external = b"external frozen target replacement\n"
    original_copy = workflow.copy_file_stable
    swapped = False

    def swap_frozen_after_copy(
        source: pathlib.Path,
        target: pathlib.Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal swapped
        result = original_copy(source, target, **kwargs)
        if workflow.same_path(target, publication_target):
            os.replace(target, publication_owned)
            write_bytes(target, publication_external)
            swapped = True
        return result

    before_publication = file_inventory(pathlib.Path(publication["oni"]))
    workflow.copy_file_stable = swap_frozen_after_copy
    try:
        try:
            workflow.finalize(workflow_finalize_namespace(publication, apply=True))
            raise VerificationFailure("frozen runtime config publication name swap was accepted")
        except workflow.WorkflowError:
            pass
    finally:
        workflow.copy_file_stable = original_copy
    assert_true(
        swapped
        and publication_target.read_bytes() == publication_external
        and publication_owned.exists(),
        "frozen config external replacement or owned copy was lost",
    )
    assert_true(
        file_inventory(pathlib.Path(publication["oni"])) == before_publication,
        "frozen config publication race reached destructive ONI restore",
    )
    results["frozen_copy_name_swap"] = "external_and_owned_copies_preserved_pretransaction"
    return results


def test_runtime_config_finalized_custody_refusals(base: pathlib.Path) -> dict[str, Any]:
    fixture, _ = ready_runtime_fixture(base, "config-finalized-custody")
    finalized = json.loads(core(fixture, "finalize", apply=True).stdout)
    assert_true(finalized.get("status") == "finalized", "custody fixture did not finalize")
    environment = fixture_paths(fixture)["environment"]
    frozen_path = environment / "finalization" / "pair" / "config.json"
    binding_path = environment / "runtime-binding.json"
    result_path = environment / "finalization" / "finalize-result.json"

    frozen_payload = frozen_path.read_bytes()
    frozen_path.unlink()
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "missing frozen config copy was accepted")
    write_bytes(frozen_path, frozen_payload)

    binding_payload = binding_path.read_bytes()
    binding = json.loads(binding_payload.decode("utf-8"))
    del binding["runtime_config"]["bootstrap"]
    write_json(binding_path, binding)
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "runtime binding missing config field was accepted")
    write_bytes(binding_path, binding_payload)

    result_payload = result_path.read_bytes()
    result = json.loads(result_payload.decode("utf-8"))
    del result["startup_binding_sha256"]
    write_json(result_path, result)
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "finalize result missing startup binding was accepted")
    write_bytes(result_path, result_payload)

    result = json.loads(result_payload.decode("utf-8"))
    del result["runtime_config"]
    write_json(result_path, result)
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "finalize result missing config binding was accepted")

    archived_startup = (
        environment / "rb" / runtime_config_contract.STARTUP_BINDING_NAME
    )
    archived_startup.unlink()
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "missing archived startup binding was accepted")
    return {
        "missing_frozen_copy": "refused",
        "missing_archived_startup_binding": "refused",
        "runtime_binding_missing_field": "refused",
        "finalize_result_missing_startup_binding": "refused",
        "finalize_result_missing_runtime_config": "refused",
    }


def _environment_bundle_path_case(
    base: pathlib.Path,
    name: str,
    replacement: Callable[[dict[str, Any], dict[str, Any], pathlib.Path], str],
    *,
    frozen: bool,
) -> str:
    fixture = make_fixture(base, name, run_id=f"g0-{name}")
    prepare_fixture(fixture)
    runtime = simulate_runtime(fixture)
    report_path = runtime["lineage"].parent / "priority-pair-finalization.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    selected, other = report["pair"]["commands"]
    value = replacement(selected, other, runtime["bridge"])
    if frozen:
        freeze_path = pathlib.Path(selected["lineage_freeze"]["path"])
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        freeze["approval_bundle"]["path"] = value
        freeze["freeze_hash"] = workflow.contract_hash_without(freeze, "freeze_hash", "path fixture freeze")
        write_json(freeze_path, freeze)
        selected["lineage_freeze"]["file_sha256"] = sha256(freeze_path)
        selected["lineage_freeze"]["freeze_hash"] = freeze["freeze_hash"]
    else:
        selected["automatic_approval_bundle"]["path"] = value
    report["report_hash"] = workflow.contract_hash_without(report, "report_hash", "path fixture report")
    write_json(report_path, report)
    fixture["pair_report_hash"] = report["report_hash"]
    before = file_inventory(pathlib.Path(fixture["oni"]))
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", f"unsafe {'freeze' if frozen else 'report'} path was accepted")
    assert_true(file_inventory(pathlib.Path(fixture["oni"])) == before, "unsafe path refusal changed fixture")
    return "infra_fail_refused"


def _environment_production_path_case(
    base: pathlib.Path,
    name: str,
    mutate: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], pathlib.Path], bool],
) -> str:
    fixture = make_fixture(base, name, run_id=f"g0-{name}")
    prepare_fixture(fixture)
    runtime = simulate_runtime(fixture)
    report_path = runtime["lineage"].parent / "priority-pair-finalization.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    selected, other = report["pair"]["commands"]
    freeze_path = pathlib.Path(selected["lineage_freeze"]["path"])
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze_changed = mutate(selected, other, freeze, runtime["bridge"])
    if freeze_changed:
        freeze["freeze_hash"] = workflow.contract_hash_without(freeze, "freeze_hash", f"{name} freeze")
        write_json(freeze_path, freeze)
        selected["lineage_freeze"]["file_sha256"] = sha256(freeze_path)
        selected["lineage_freeze"]["freeze_hash"] = freeze["freeze_hash"]
    report["report_hash"] = workflow.contract_hash_without(report, "report_hash", f"{name} report")
    write_json(report_path, report)
    fixture["pair_report_hash"] = report["report_hash"]
    result = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(result.get("status") == "blocked", f"unsafe production {name} path was accepted")
    return "infra_fail_refused"


def test_production_bridge_relative_path_contract(base: pathlib.Path) -> dict[str, Any]:
    report_escape = _environment_bundle_path_case(
        base,
        "env-report-path-escape",
        lambda _selected, _other, _bridge: "../outside/approval-bundle.json",
        frozen=False,
    )
    report_absolute = _environment_bundle_path_case(
        base,
        "env-report-path-absolute",
        lambda _selected, _other, bridge: str(bridge.parent / "wrong-root" / "approval-bundle.json"),
        frozen=False,
    )
    report_swap = _environment_bundle_path_case(
        base,
        "env-report-path-swap",
        lambda _selected, other, _bridge: str(other["automatic_approval_bundle"]["path"]),
        frozen=False,
    )
    freeze_escape = _environment_bundle_path_case(
        base,
        "env-freeze-path-escape",
        lambda _selected, _other, _bridge: "../outside/approval-bundle.json",
        frozen=True,
    )
    command_swap = _environment_production_path_case(
        base,
        "env-command-file-swap",
        lambda selected, other, _freeze, _bridge: (
            selected.__setitem__("file", other["file"]) or False
        ),
    )
    frame_escape = _environment_production_path_case(
        base,
        "env-frame-path-escape",
        lambda selected, _other, _freeze, _bridge: (
            selected["source_frame"].__setitem__("archive_file", "../outside/frame.json") or False
        ),
    )
    certificate_absolute = _environment_production_path_case(
        base,
        "env-report-cert-absolute",
        lambda selected, _other, _freeze, bridge: (
            selected["confirmation"]["certificate"].__setitem__(
                "path", str(bridge.parent / "wrong-root" / "action-outcomes.json")
            )
            or False
        ),
    )
    frozen_certificate_escape = _environment_production_path_case(
        base,
        "env-freeze-cert-escape",
        lambda _selected, _other, freeze, _bridge: (
            freeze["confirmation_certificate"].__setitem__("path", "../outside/action-outcomes.json")
            or True
        ),
    )
    return {
        "positive_shape": "bridge_relative_fixture_used_by_success_finalize",
        "reported_escape": report_escape,
        "reported_absolute_wrong_root": report_absolute,
        "reported_forward_inverse_swap": report_swap,
        "frozen_escape": freeze_escape,
        "done_command_cross_swap": command_swap,
        "source_frame_escape": frame_escape,
        "reported_certificate_absolute_wrong_root": certificate_absolute,
        "frozen_certificate_escape": frozen_certificate_escape,
    }


def test_evicted_sources_and_confirmation_semantics(base: pathlib.Path) -> dict[str, Any]:
    evicted = make_fixture(base, "evicted-frame-certificate", run_id="g0-evicted-sources")
    prepare_fixture(evicted)
    simulate_runtime(evicted, evict_forward_frame=True, evict_forward_certificate=True)
    planned = json.loads(core(evicted, "finalize", what_if=True).stdout)
    assert_true(planned.get("status") == "would_finalize", "frozen frame/certificate fallback was rejected")

    false_present = make_fixture(base, "frame-false-but-present", run_id="g0-frame-false-present")
    prepare_fixture(false_present)
    false_runtime = simulate_runtime(false_present)
    false_report_path = false_runtime["lineage"].parent / "priority-pair-finalization.json"
    false_report = json.loads(false_report_path.read_text(encoding="utf-8"))
    false_report["pair"]["commands"][0]["source_frame"]["archive_currently_present"] = False
    false_report["report_hash"] = workflow.contract_hash_without(
        false_report, "report_hash", "false-present report"
    )
    write_json(false_report_path, false_report)
    false_present["pair_report_hash"] = false_report["report_hash"]
    blocked = json.loads(core(false_present, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "false-but-present frame was accepted")

    frame_drift = make_fixture(base, "frame-present-drift", run_id="g0-frame-present-drift")
    prepare_fixture(frame_drift)
    frame_runtime = simulate_runtime(frame_drift)
    frame_report = json.loads(
        (frame_runtime["lineage"].parent / "priority-pair-finalization.json").read_text(encoding="utf-8")
    )
    frame_path = frame_runtime["bridge"] / frame_report["pair"]["commands"][0]["source_frame"]["archive_file"]
    frame_document = json.loads(frame_path.read_text(encoding="utf-8"))
    frame_document["tampered"] = True
    write_json(frame_path, frame_document)
    blocked = json.loads(core(frame_drift, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "drifted present frame was accepted")

    certificate_drift = make_fixture(base, "certificate-present-drift", run_id="g0-cert-present-drift")
    prepare_fixture(certificate_drift)
    certificate_runtime = simulate_runtime(certificate_drift)
    certificate_freeze_path = (
        certificate_runtime["lineage"].parent / "priority-command-freezes" / "priority_forward.json"
    )
    certificate_freeze = json.loads(certificate_freeze_path.read_text(encoding="utf-8"))
    certificate_path = certificate_runtime["bridge"] / certificate_freeze["confirmation_certificate"]["path"]
    certificate_document = json.loads(certificate_path.read_text(encoding="utf-8"))
    certificate_document["tampered"] = True
    certificate_document["outcomes_hash"] = workflow.contract_hash_without(
        certificate_document, "outcomes_hash", "drifted certificate"
    )
    write_json(certificate_path, certificate_document)
    blocked = json.loads(core(certificate_drift, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "drifted present certificate was accepted")

    def confirmation_case(
        name: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        certificate_changed: bool,
    ) -> str:
        fixture = make_fixture(base, name, run_id=f"g0-{name}")
        prepare_fixture(fixture)
        runtime = simulate_runtime(fixture)
        report_path = runtime["lineage"].parent / "priority-pair-finalization.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        selected = report["pair"]["commands"][0]
        freeze_path = pathlib.Path(selected["lineage_freeze"]["path"])
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        mutate(freeze)
        if certificate_changed:
            certificate = freeze["confirmation_certificate"]["document"]
            certificate["outcomes_hash"] = workflow.contract_hash_without(
                certificate, "outcomes_hash", f"{name} certificate"
            )
            freeze["confirmation_certificate"]["outcomes_hash"] = certificate["outcomes_hash"]
            certificate_path = runtime["bridge"] / freeze["confirmation_certificate"]["path"]
            write_json(certificate_path, certificate)
            freeze["confirmation_certificate"]["file_sha256"] = sha256(certificate_path)
            selected["confirmation"]["certificate"]["file_sha256"] = sha256(certificate_path)
            selected["confirmation"]["certificate"]["outcomes_hash"] = certificate["outcomes_hash"]
        freeze["freeze_hash"] = workflow.contract_hash_without(freeze, "freeze_hash", f"{name} freeze")
        write_json(freeze_path, freeze)
        selected["lineage_freeze"]["file_sha256"] = sha256(freeze_path)
        selected["lineage_freeze"]["freeze_hash"] = freeze["freeze_hash"]
        report["report_hash"] = workflow.contract_hash_without(report, "report_hash", f"{name} report")
        write_json(report_path, report)
        fixture["pair_report_hash"] = report["report_hash"]
        result = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
        assert_true(result.get("status") == "blocked", f"{name} confirmation tamper was accepted")
        return "production_assessor_refused"

    wrong_command = confirmation_case(
        "confirmation-wrong-command",
        lambda freeze: freeze["confirmation_certificate"]["document"]["outcomes"][0].update(
            {"command_id": "cmd-wrong-certificate-fixture"}
        ),
        certificate_changed=True,
    )
    bad_attestation = confirmation_case(
        "confirmation-bad-attestation",
        lambda freeze: freeze["confirmation_certificate"]["document"]["outcomes"][0].update(
            {"attestation_hash": "sha256:" + "0" * 64}
        ),
        certificate_changed=True,
    )
    bad_ack = confirmation_case(
        "confirmation-bad-ack",
        lambda freeze: freeze["confirmation_records"][0].update({"seq": 999}),
        certificate_changed=False,
    )
    bad_witness = confirmation_case(
        "confirmation-bad-witness",
        lambda freeze: freeze["confirmation_records"][-1].update(
            {"save_id_hash": "sha256:" + "9" * 64}
        ),
        certificate_changed=False,
    )
    return {
        "evicted_frame_and_certificate": "frozen_fallback_accepted",
        "false_but_present_frame": "refused",
        "present_frame_drift": "refused",
        "present_certificate_drift": "refused",
        "wrong_command": wrong_command,
        "bad_attestation": bad_attestation,
        "bad_ack": bad_ack,
        "bad_witness": bad_witness,
    }


def test_shutdown_after_verify_name_swap(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "shutdown-after-verify-swap", run_id="g0-shutdown-after-verify-swap")
    prepare_fixture(fixture)
    runtime = simulate_runtime(fixture)
    arguments = [
        "finalize",
        *common_args(fixture),
        *finalize_binding_args(fixture),
        "--what-if",
    ]
    args = workflow.build_parser().parse_args(arguments)
    roots = workflow.resolve_roots(args)
    source = runtime["shutdown_event_log"]
    source_stamp = source.stat().st_mtime_ns
    original_size = source.stat().st_size
    moved_owner = source.with_name("state.owner-before-swap.jsonl")

    def swap_source(_verified: dict[str, Any]) -> None:
        records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        records[-1]["mod_version"] = "9.9.9"
        replacement = source.with_name("state.replacement.jsonl")
        write_jsonl(replacement, records)
        assert_true(replacement.stat().st_size == original_size, "swap fixture did not preserve byte length")
        os.utime(replacement, ns=(source_stamp, source_stamp))
        os.replace(source, moved_owner)
        os.replace(replacement, source)

    try:
        workflow.finalize_locked(args, roots, after_shutdown_verify_hook=swap_source)
        raise VerificationFailure("after-verify source name swap was accepted")
    except workflow.WorkflowError as exc:
        blocker = str(exc)
    assert_true(
        "shutdown final tip source lost its exact evidence binding" in blocker
        or "shutdown source log" in blocker
        or "normal shutdown evidence" in blocker,
        "swap failed outside shutdown custody gate",
    )
    assert_true(not (fixture_paths(fixture)["environment"] / "runtime-binding.json").exists(), "swap refusal published runtime binding")

    startup_fixture = make_fixture(base, "startup-after-verify-swap", run_id="g0-startup-after-swap")
    prepare_fixture(startup_fixture)
    simulate_runtime(startup_fixture)
    startup_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(startup_fixture),
            *finalize_binding_args(startup_fixture),
            "--what-if",
        ]
    )
    startup_roots = workflow.resolve_roots(startup_args)
    startup_report = pathlib.Path(startup_fixture["startup_consumption_bundle"]) / startup_contract.REPORT_FILE
    startup_owner = pathlib.Path(startup_fixture["case"]) / "startup-report-owned-before-swap.json"

    def swap_startup_bundle_file(_verified: dict[str, Any]) -> None:
        os.replace(startup_report, startup_owner)
        shutil.copy2(startup_owner, startup_report)

    try:
        workflow.finalize_locked(
            startup_args,
            startup_roots,
            after_shutdown_verify_hook=swap_startup_bundle_file,
        )
        raise VerificationFailure("after-verify startup bundle identity swap was accepted")
    except workflow.WorkflowError as exc:
        startup_blocker = str(exc)
    assert_true(
        "startup consumption bundle custody changed" in startup_blocker,
        "startup name swap failed outside the custody identity gate",
    )
    assert_true(
        not (fixture_paths(startup_fixture)["environment"] / "runtime-binding.json").exists(),
        "startup name-swap refusal published runtime binding",
    )
    return {
        "same_length_same_mtime_name_swap": "refused_after_initial_verify",
        "startup_same_bytes_same_mtime_inode_swap": "refused_after_initial_verify",
        "runtime_snapshot_cross_binding": "enforced",
        "runtime_binding_published": False,
    }


def test_pipeline_final_bridge_tree_sandwich(base: pathlib.Path) -> dict[str, Any]:
    outcomes: dict[str, str] = {}
    for label, relative in (
        ("health", pathlib.PurePosixPath("health.json")),
        ("companion", pathlib.PurePosixPath("advice.json")),
        ("root_log", pathlib.PurePosixPath("state.jsonl")),
    ):
        fixture = make_fixture(base, f"pfs-{label}", run_id=f"g0-pfs-{label}")
        prepare_fixture(fixture)
        runtime = simulate_runtime(fixture)
        arguments = [
            "finalize",
            *common_args(fixture),
            *finalize_binding_args(fixture),
            "--what-if",
        ]
        args = workflow.build_parser().parse_args(arguments)
        roots = workflow.resolve_roots(args)
        bridge = pathlib.Path(runtime["bridge"])
        target = bridge.joinpath(*relative.parts)
        after_hook_inventory: dict[str, dict[str, Any]] | None = None

        def mutate_after_semantic_validation(_bridge: pathlib.Path) -> None:
            nonlocal after_hook_inventory
            assert_true(_bridge == bridge, "pipeline sandwich hook received the wrong Bridge")
            write_bytes(target, target.read_bytes() + b"\n")
            after_hook_inventory = file_inventory(pathlib.Path(fixture["oni"]))

        try:
            workflow.finalize_locked(
                args,
                roots,
                before_pipeline_final_verify_hook=mutate_after_semantic_validation,
            )
            raise VerificationFailure(f"late {label} rewrite escaped the full Bridge tree sandwich")
        except workflow.WorkflowError as exc:
            blocker = str(exc)
        assert_true(
            "terminal Bridge pipeline tree no longer matches" in blocker,
            f"late {label} rewrite failed outside the full Bridge tree guard: {blocker}",
        )
        assert_true(after_hook_inventory is not None, f"late {label} hook did not run")
        assert_true(
            file_inventory(pathlib.Path(fixture["oni"])) == after_hook_inventory,
            f"late {label} refusal performed an additional physical write",
        )
        assert_true(
            not (fixture_paths(fixture)["environment"] / "runtime-binding.json").exists(),
            f"late {label} refusal published runtime binding",
        )
        outcomes[label] = "refused_by_same_full_tree_guard"
    return {
        **outcomes,
        "physical_writes_after_hook": 0,
        "runtime_binding_published": False,
    }


def test_active_parent_finalize_lock_manager_custody(
    base: pathlib.Path,
) -> dict[str, Any]:
    """Exercise the manager-only descriptor proof and nested lock scoping."""

    def configured(
        name: str,
    ) -> tuple[
        dict[str, pathlib.Path | str],
        Any,
        Any,
        Any,
    ]:
        fixture = make_fixture(base, name, run_id=f"g0-{name}")
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)
        args = workflow.build_parser().parse_args(
            [
                "abort",
                *common_args(fixture),
                *abort_binding_args(fixture),
                "--apply",
            ]
        )
        roots = workflow.resolve_roots(args)
        namespace = workflow._runtime_config_namespace(
            args,
            expected_environment_manifest_sha256=sha256(
                fixture_paths(fixture)["manifest"]
            ),
        )
        return fixture, args, roots, namespace

    def expect_refused(call: Callable[[], Any], label: str) -> str:
        try:
            call()
        except runtime_config_contract.ConfigError as exc:
            return str(exc)
        raise VerificationFailure(f"{label} active-parent-lock proof was accepted")

    def assert_no_finalize_publication(
        fixture: dict[str, pathlib.Path | str],
    ) -> None:
        environment = fixture_paths(fixture)["environment"]
        forbidden = (
            environment / "runtime-binding.json",
            environment / "finalization" / "finalize-result.json",
            environment / "abort" / "abort-result.json",
        )
        assert_true(
            not any(os.path.lexists(path) for path in forbidden),
            "active-parent-lock refusal published a finalize/abort result",
        )
        journal = environment / "restore-journal.jsonl"
        if journal.is_file():
            events = [entry.get("event") for entry in workflow.verify_journal(journal)]
            assert_true(
                "restore_transaction_physical_committed" not in events,
                "active-parent-lock refusal replayed a physical finalize/abort transaction",
            )

    closed, _closed_args, closed_roots, closed_namespace = configured("pfl-fd")
    closed_before = file_inventory(pathlib.Path(closed["case"]))
    with workflow.exclusive_finalize_lock(closed_roots) as valid_proof:
        assert_true(
            workflow._ACTIVE_PARENT_FINALIZE_LOCK.get() is valid_proof,
            "manager did not scope the active parent lock proof",
        )
        verified = runtime_config_contract.verify_evidence(
            closed_namespace,
            active_parent_finalize_lock=valid_proof,
        )
        assert_true(verified.get("status") == "passed", "valid parent lock proof was refused")
        no_proof_error = expect_refused(
            lambda: runtime_config_contract.verify_evidence(closed_namespace),
            "missing",
        )
        duplicate_fd = os.dup(valid_proof.fd)
        os.close(duplicate_fd)
        closed_proof = replace(valid_proof, fd=duplicate_fd)
        closed_fd_error = expect_refused(
            lambda: runtime_config_contract.verify_evidence(
                closed_namespace,
                active_parent_finalize_lock=closed_proof,
            ),
            "closed descriptor",
        )
    assert_true(
        workflow._ACTIVE_PARENT_FINALIZE_LOCK.get() is None
        and file_inventory(pathlib.Path(closed["case"])) == closed_before,
        "parent lock scope/closed-descriptor refusal changed the fixture",
    )
    assert_no_finalize_publication(closed)

    reused, _reused_args, reused_roots, reused_namespace = configured("pfl-reuse")
    decoy = pathlib.Path(reused["case"]) / "fd-reuse-decoy.bin"
    write_bytes(decoy, b"foreign-descriptor-reuse")
    reused_before = file_inventory(pathlib.Path(reused["case"]))
    with workflow.exclusive_finalize_lock(reused_roots) as valid_proof:
        duplicate_fd = os.dup(valid_proof.fd)
        os.close(duplicate_fd)
        replacement_fd = os.open(
            decoy,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        try:
            assert_true(
                replacement_fd == duplicate_fd,
                "fixture could not deterministically reuse the closed descriptor number",
            )
            reused_proof = replace(valid_proof, fd=replacement_fd)
            fd_reuse_error = expect_refused(
                lambda: runtime_config_contract.verify_evidence(
                    reused_namespace,
                    active_parent_finalize_lock=reused_proof,
                ),
                "descriptor reuse",
            )
        finally:
            os.close(replacement_fd)
    assert_true(
        decoy.read_bytes() == b"foreign-descriptor-reuse"
        and file_inventory(pathlib.Path(reused["case"])) == reused_before,
        "descriptor-reuse refusal changed or removed the foreign file",
    )
    assert_no_finalize_publication(reused)

    first, _first_args, first_roots, first_namespace = configured("pfl-a")
    second, _second_args, second_roots, second_namespace = configured("pfl-b")
    first_before = file_inventory(pathlib.Path(first["case"]))
    second_before = file_inventory(pathlib.Path(second["case"]))
    with workflow.exclusive_finalize_lock(first_roots) as first_proof:
        assert_true(
            workflow._ACTIVE_PARENT_FINALIZE_LOCK.get() is first_proof,
            "first parent lock proof was not current",
        )
        with workflow.exclusive_finalize_lock(second_roots) as second_proof:
            assert_true(
                workflow._ACTIVE_PARENT_FINALIZE_LOCK.get() is second_proof,
                "nested parent lock proof was not current",
            )
            two_lock_error = expect_refused(
                lambda: runtime_config_contract.verify_evidence(
                    second_namespace,
                    active_parent_finalize_lock=first_proof,
                ),
                "cross-environment",
            )
            assert_true(
                runtime_config_contract.verify_evidence(
                    first_namespace,
                    active_parent_finalize_lock=first_proof,
                ).get("status")
                == "passed"
                and runtime_config_contract.verify_evidence(
                    second_namespace,
                    active_parent_finalize_lock=second_proof,
                ).get("status")
                == "passed",
                "independent parent lock proofs did not remain environment-scoped",
            )
        assert_true(
            workflow._ACTIVE_PARENT_FINALIZE_LOCK.get() is first_proof,
            "nested parent lock scope did not restore the outer proof",
        )
    assert_true(
        workflow._ACTIVE_PARENT_FINALIZE_LOCK.get() is None
        and file_inventory(pathlib.Path(first["case"])) == first_before
        and file_inventory(pathlib.Path(second["case"])) == second_before,
        "two-environment lock proof test changed either fixture",
    )
    assert_no_finalize_publication(first)
    assert_no_finalize_publication(second)

    return {
        "valid_parent_proof": "accepted_read_only",
        "no_proof": "refused_while_parent_lock_named",
        "closed_fd": "refused_zero_finalize_replay",
        "descriptor_reuse": "refused_foreign_file_retained",
        "two_environment_locks": "cross_proof_refused_nested_scope_exact",
        "error_evidence": {
            "no_proof": no_proof_error,
            "closed_fd": closed_fd_error,
            "descriptor_reuse": fd_reuse_error,
            "two_lock": two_lock_error,
        },
        "finalize_or_abort_results_published": 0,
    }


def test_finalize_lock_replacement_retained(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "finalize-lock-replacement", run_id="g0-finalize-lock-replacement")
    prepare_fixture(fixture)
    roots = workflow.resolve_roots(
        workflow.build_parser().parse_args(["prepare", *common_args(fixture), "--what-if"])
    )
    lock_path = roots.environment_dir / ".finalize.lock"
    replacement = roots.environment_dir / ".external-lock-replacement"
    moved_owner = roots.environment_dir / ".owned-lock-moved"
    replacement_payload = b'external-lock-replacement-must-survive\n'
    write_bytes(replacement, replacement_payload)

    def replace_before_release(path: pathlib.Path) -> None:
        os.replace(path, moved_owner)
        os.replace(replacement, path)

    try:
        with workflow.exclusive_finalize_lock(roots, before_release_hook=replace_before_release):
            pass
        raise VerificationFailure("replacement finalize lock was unlinked")
    except workflow.WorkflowError:
        blocker = "ownership_token_identity_refused"
    assert_true(lock_path.read_bytes() == replacement_payload, "external lock replacement was deleted or changed")
    assert_true(moved_owner.exists(), "owned lock inode was not distinguishable from replacement")

    late_fixture = make_fixture(base, "finalize-lock-late-replacement", run_id="g0-finalize-lock-late")
    prepare_fixture(late_fixture)
    late_roots = workflow.resolve_roots(
        workflow.build_parser().parse_args(["prepare", *common_args(late_fixture), "--what-if"])
    )
    late_lock = late_roots.environment_dir / ".finalize.lock"
    late_replacement = late_roots.environment_dir / ".external-late-lock"
    late_owner = late_roots.environment_dir / ".owned-late-lock"
    late_payload = b"late-external-lock-must-survive\n"
    write_bytes(late_replacement, late_payload)

    def replace_after_verify(path: pathlib.Path) -> None:
        os.replace(path, late_owner)
        os.replace(late_replacement, path)

    try:
        with workflow.exclusive_finalize_lock(
            late_roots, after_verify_before_unlink_hook=replace_after_verify
        ):
            pass
        raise VerificationFailure("late finalize lock replacement was unlinked")
    except workflow.WorkflowError:
        late_blocker = "atomic_reclaim_identity_refused"
    assert_true(late_lock.read_bytes() == late_payload, "late external lock replacement was deleted")
    assert_true(late_owner.exists(), "late owned lock was not retained")
    for candidate in (fixture, late_fixture):
        environment = fixture_paths(candidate)["environment"]
        assert_true(
            not (environment / "runtime-binding.json").exists()
            and not (environment / "finalization" / "finalize-result.json").exists()
            and not (environment / "abort" / "abort-result.json").exists(),
            "finalize lock release-swap refusal published a result",
        )
    return {
        "replacement": blocker,
        "after_verify_before_unlink": late_blocker,
        "release_window_swap": "refused_zero_finalize_replay",
        "external_replacement_retained": True,
        "finalize_or_abort_results_published": 0,
    }


def test_pipeline_outcome_unknown_refusal(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "pipeline-outcome-unknown", run_id="g0-outcome-unknown")
    prepare_fixture(fixture)
    runtime = simulate_runtime(fixture)
    failed = runtime["bridge"] / "commands" / "failed" / "cmd-unresolved-fixture.json"
    write_json(
        failed,
        {
            "schema": "oni.command.v3",
            "id": "cmd-unresolved-fixture",
            "status": "failed",
            "action": "set_duplicant_chore_priority",
            "save_id_hash": sha256(runtime["derivative"]),
            "result": {"classification": "outcome_unknown"},
        },
    )
    latest_state = json.loads((runtime["bridge"] / "latest_state.json").read_text(encoding="utf-8"))
    policy = json.loads((runtime["bridge"] / "action-policy.json").read_text(encoding="utf-8"))
    readiness = workflow.command_pipeline_readiness(
        runtime["bridge"],
        latest_state=latest_state,
        policy=policy,
    )
    ambiguous = readiness.get("ambiguous_outcomes") or []
    assert_true(
        len(ambiguous) == 1 and ambiguous[0].get("classification") == "outcome_unknown",
        "fixture did not produce the expected outcome_unknown pipeline latch",
    )
    before = file_inventory(pathlib.Path(fixture["oni"]))
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "outcome_unknown pipeline latch was accepted")
    assert_true(file_inventory(pathlib.Path(fixture["oni"])) == before, "pipeline refusal changed the fixture")
    return {
        "pipeline_schema": readiness.get("schema"),
        "mutation_proposal_ready": readiness.get("readiness", {}).get("mutation_proposal", {}).get("ready"),
        "ambiguous_outcome_count": 1,
        "classification": "outcome_unknown",
        "finalize": "infra_fail_refused",
    }


def test_pipeline_frozen_input_tamper_refusals(base: pathlib.Path) -> dict[str, Any]:
    outcomes: dict[str, str] = {}

    for label, relative in (
        ("latest", pathlib.PurePosixPath("latest_state.json")),
        ("health", pathlib.PurePosixPath("health.json")),
        ("companion", pathlib.PurePosixPath("advice.json")),
        ("policy", pathlib.PurePosixPath("action-policy.json")),
    ):
        fixture = make_fixture(base, f"pipe-{label}", run_id=f"g0-pipe-{label}")
        prepare_fixture(fixture)
        runtime = simulate_runtime(fixture)
        target = pathlib.Path(runtime["bridge"]).joinpath(*relative.parts)
        write_bytes(target, target.read_bytes() + b"\n")
        before = file_inventory(pathlib.Path(fixture["oni"]))
        blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
        assert_true(blocked.get("status") == "blocked", f"changed {label} bytes were accepted")
        assert_true(
            file_inventory(pathlib.Path(fixture["oni"])) == before,
            f"changed {label} refusal performed a physical write",
        )
        outcomes[label] = "exact_frozen_input_refused"

    unknown = make_fixture(base, "pipe-log-extra", run_id="g0-pipe-log-extra")
    prepare_fixture(unknown)
    unknown_runtime = simulate_runtime(unknown)
    write_json(
        pathlib.Path(unknown_runtime["bridge"]) / "state.jsonl.unrecognized",
        {"schema": "oni.test-unrecognized-state-log.v1"},
    )
    unknown_before = file_inventory(pathlib.Path(unknown["oni"]))
    blocked = json.loads(core(unknown, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "unrecognized state-log suffix was accepted")
    assert_true(
        file_inventory(pathlib.Path(unknown["oni"])) == unknown_before,
        "unrecognized state-log refusal performed a physical write",
    )
    outcomes["unrecognized_state_log_suffix"] = "complete_path_set_refused"

    startup = make_fixture(base, "pipe-startup-hash", run_id="g0-pipe-startup-hash")
    prepare_fixture(startup)
    startup_runtime = simulate_runtime(startup)
    pair_path = pathlib.Path(startup_runtime["lineage"]).parent / "priority-pair-finalization.json"
    pair = json.loads(pair_path.read_text(encoding="utf-8"))
    forged_startup_hash = "sha256:" + "f" * 64
    forged_pipeline = copy.deepcopy(pair["shutdown"]["command_pipeline"])
    forged_pipeline["inputs"]["bridge_replay"]["startup_expectations_hash"] = forged_startup_hash
    startup_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(startup),
            *finalize_binding_args(startup),
            "--what-if",
        ]
    )
    startup_roots = workflow.resolve_roots(startup_args)
    _, startup_manifest, startup_manifest_hash = workflow.load_environment_manifest(
        startup_args,
        startup_roots,
    )
    startup_runtime_config = workflow.validate_runtime_config_evidence(
        startup_args,
        startup_roots,
        startup_manifest,
        startup_manifest_hash,
        verify_current_installation=False,
    )
    startup_shutdown = json.loads(
        pathlib.Path(startup_runtime["shutdown_evidence"]).read_text(encoding="utf-8")
    )
    startup_certificate = startup_shutdown["startup_consumption_certificate"]

    typed_pipeline = copy.deepcopy(pair["shutdown"]["command_pipeline"])
    assert_true(
        typed_pipeline["inputs"]["terminal_publication"] is True,
        "fixture terminal publication was not a boolean",
    )
    typed_pipeline["inputs"]["terminal_publication"] = 1
    typed_before = file_inventory(pathlib.Path(startup["oni"]))
    try:
        workflow.command_pipeline_gate(
            pathlib.Path(startup_runtime["bridge"]),
            expected_save_id_hash=sha256(pathlib.Path(startup_runtime["derivative"])),
            frozen_pipeline=typed_pipeline,
            expected_final_tip=pair["final_tip"],
            shutdown_document=startup_shutdown,
            expected_run_id=str(startup["run_id"]),
            runtime_config=startup_runtime_config,
            startup_certificate=startup_certificate,
        )
        raise VerificationFailure("bool/int-substituted frozen pipeline was accepted")
    except workflow.WorkflowError as exc:
        typed_blocker = str(exc)
    assert_true(
        "frozen pair command pipeline does not match current exact inputs" in typed_blocker,
        f"bool/int pipeline substitution failed outside strict canonical equality: {typed_blocker}",
    )
    assert_true(
        file_inventory(pathlib.Path(startup["oni"])) == typed_before,
        "bool/int pipeline substitution refusal performed a physical write",
    )
    outcomes["bool_int_nested_substitution"] = "strict_canonical_equality_refused"

    original_replay = workflow.priority_pair_contract._bounded_bridge_replay

    def forged_current_replay(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        replay, snapshots = original_replay(*args, **kwargs)
        forged = copy.deepcopy(replay)
        forged["startup_expectations_hash"] = forged_startup_hash
        return forged, snapshots

    direct_before = file_inventory(pathlib.Path(startup["oni"]))
    workflow.priority_pair_contract._bounded_bridge_replay = forged_current_replay
    try:
        try:
            workflow.command_pipeline_gate(
                pathlib.Path(startup_runtime["bridge"]),
                expected_save_id_hash=sha256(pathlib.Path(startup_runtime["derivative"])),
                frozen_pipeline=forged_pipeline,
                expected_final_tip=pair["final_tip"],
                shutdown_document=startup_shutdown,
                expected_run_id=str(startup["run_id"]),
                runtime_config=startup_runtime_config,
                startup_certificate=startup_certificate,
            )
            raise VerificationFailure("coherently forged current/frozen startup replay hash was accepted")
        except workflow.WorkflowError as exc:
            direct_blocker = str(exc)
    finally:
        workflow.priority_pair_contract._bounded_bridge_replay = original_replay
    assert_true(
        "Bridge replay startup expectations were not independently anchored" in direct_blocker,
        f"coherent current/frozen startup replay forgery failed outside the external anchor: {direct_blocker}",
    )
    assert_true(
        file_inventory(pathlib.Path(startup["oni"])) == direct_before,
        "coherent current/frozen startup replay refusal performed a physical write",
    )
    pair["shutdown"]["command_pipeline"]["inputs"]["bridge_replay"][
        "startup_expectations_hash"
    ] = forged_startup_hash
    pair["report_hash"] = workflow.contract_hash_without(pair, "report_hash", "fixture pair report")
    write_json(pair_path, pair)
    startup["pair_report_hash"] = pair["report_hash"]
    rebuilt_shutdown = shutdown_contract.build_shutdown_evidence(
        manifest_path=pathlib.Path(startup_runtime["lineage"]),
        bridge_dir=pathlib.Path(startup_runtime["bridge"]),
        pair_report_path=pair_path,
        expected_pair_report_hash=pair["report_hash"],
        expected_run_id=str(startup["run_id"]),
        forward_step="priority_forward",
        inverse_step="priority_inverse",
        command_ids=["cmd-priority-forward-fixture", "cmd-priority-inverse-fixture"],
        command_bundle_hashes=[
            str(startup["forward_bundle_hash"]),
            str(startup["inverse_bundle_hash"]),
        ],
        event_log=pathlib.Path(startup_runtime["final_event_log"]),
        startup_bundle_dir=pathlib.Path(startup["startup_consumption_bundle"]),
        expected_startup_evidence_hash=str(startup["startup_consumption_evidence_hash"]),
        expected_validation_process_id=int(startup["validation_process_id"]),
    )
    write_json(pathlib.Path(startup_runtime["shutdown_evidence"]), rebuilt_shutdown)
    startup["shutdown_evidence_hash"] = rebuilt_shutdown["evidence_hash"]
    startup_before = file_inventory(pathlib.Path(startup["oni"]))
    blocked = json.loads(core(startup, "finalize", what_if=True, expected={2}).stdout)
    assert_true(
        blocked.get("status") == "blocked",
        "coherently rewrapped startup expectations hash was accepted",
    )
    assert_true(
        file_inventory(pathlib.Path(startup["oni"])) == startup_before,
        "startup expectations refusal performed a physical write",
    )
    outcomes["startup_expectations_hash"] = "external_runtime_certificate_anchor_refused"
    outcomes["coherent_current_and_frozen_startup_hash"] = "independent_anchor_refused"
    return {**outcomes, "physical_writes_after_tamper": 0}


def test_cleanup_pipeline_freshness_window(base: pathlib.Path) -> dict[str, Any]:
    accepted_fixture = make_fixture(base, "cleanup-age-accepted", run_id="g0-cleanup-age-accepted")
    prepare_fixture(accepted_fixture)
    accepted_runtime = simulate_runtime(accepted_fixture)
    accepted_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(accepted_fixture),
            *finalize_binding_args(accepted_fixture),
            "--what-if",
        ]
    )
    accepted_roots = workflow.resolve_roots(accepted_args)
    freshness_calls: list[str] = []
    original_freshness = workflow.snapshot_freshness

    def frozen_pair_time_only(
        state: dict[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> tuple[float | None, float | None, list[str]]:
        assert_true(now is not None, "post-shutdown finalizer consulted current wall-clock freshness")
        freshness_calls.append(now.isoformat(timespec="microseconds"))
        return original_freshness(state, now=now)

    before_plan = file_inventory(pathlib.Path(accepted_fixture["oni"]))
    workflow.snapshot_freshness = frozen_pair_time_only
    try:
        plan = workflow.finalize_locked(accepted_args, accepted_roots)
    finally:
        workflow.snapshot_freshness = original_freshness
    assert_true(plan.get("status") == "would_finalize", "frozen pair-time cleanup proof was rejected")
    pair_report = json.loads(
        (pathlib.Path(accepted_runtime["lineage"]).parent / "priority-pair-finalization.json").read_text(
            encoding="utf-8"
        )
    )
    frozen_checked_at = pair_report["shutdown"]["command_pipeline"]["inputs"]["snapshot_freshness"]["checked_at"]
    assert_true(
        freshness_calls == [frozen_checked_at],
        "post-shutdown finalizer did not use exactly the frozen pair-time clock",
    )
    assert_true(file_inventory(pathlib.Path(accepted_fixture["oni"])) == before_plan, "accepted cleanup plan wrote files")

    expired_fixture = make_fixture(base, "cleanup-age-expired", run_id="g0-cleanup-age-expired")
    prepare_fixture(expired_fixture)
    expired_runtime = simulate_runtime(expired_fixture)
    latest_state = json.loads((expired_runtime["bridge"] / "latest_state.json").read_text(encoding="utf-8"))
    policy = json.loads((expired_runtime["bridge"] / "action-policy.json").read_text(encoding="utf-8"))
    final_records = [
        json.loads(line)
        for line in pathlib.Path(expired_runtime["final_event_log"]).read_text(encoding="utf-8").splitlines()
    ]
    raw_state = [record for record in final_records if record.get("event") == "state_snapshot"][-1]
    raw_time = datetime.fromisoformat(str(raw_state["ts"]).replace("Z", "+00:00"))
    original_pair_clock = workflow.priority_pair_contract._utc_now
    workflow.priority_pair_contract._utc_now = lambda: raw_time + timedelta(seconds=130)
    before_refusal = file_inventory(pathlib.Path(expired_fixture["oni"]))
    try:
        try:
            workflow.priority_pair_contract._command_pipeline_evidence(
                expired_runtime["bridge"],
                latest_state,
                policy,
                raw_state=raw_state,
            )
            raise VerificationFailure("pair generator accepted a 130-second-old creation tip")
        except workflow.priority_pair_contract.PriorityPairError as exc:
            blocker = exc.code
    finally:
        workflow.priority_pair_contract._utc_now = original_pair_clock
    assert_true(
        blocker == "command_pipeline_snapshot_not_fresh",
        f"expired pair creation failed outside its freshness gate: {blocker}",
    )
    assert_true(file_inventory(pathlib.Path(expired_fixture["oni"])) == before_refusal, "expired cleanup refusal wrote files")
    return {
        "cleanup_only_max_age_seconds": 120,
        "post_shutdown_wall_clock": "not_reconsulted",
        "frozen_checked_at_call_count": len(freshness_calls),
        "age_130_seconds_at_pair_creation": "infra_fail_refused",
        "process_counts_required_zero": True,
    }


def test_bridge_archive_integrity_refusals(base: pathlib.Path) -> dict[str, Any]:
    extra_fixture = make_fixture(base, "archive-unexpected-extra", run_id="g0-archive-extra")
    prepare_fixture(extra_fixture)
    simulate_runtime(extra_fixture)
    extra_path = pathlib.Path(extra_fixture["oni"]) / "ai-bridge-archives" / "unexpected" / "extra.json"
    write_json(extra_path, {"unexpected": True})
    before_extra_refusal = file_inventory(pathlib.Path(extra_fixture["oni"]))
    blocked = json.loads(core(extra_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "unexpected bridge archive entry was accepted")
    assert_true(
        file_inventory(pathlib.Path(extra_fixture["oni"])) == before_extra_refusal,
        "unexpected archive refusal changed the fixture",
    )

    changed_fixture = make_fixture(base, "archive-preexisting-changed", run_id="g0-archive-changed")
    prepare_fixture(changed_fixture)
    simulate_runtime(changed_fixture)
    write_bytes(pathlib.Path(changed_fixture["preexisting_archive_file"]), b'{"historical":"changed"}\n')
    before_changed_refusal = file_inventory(pathlib.Path(changed_fixture["oni"]))
    blocked = json.loads(core(changed_fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "changed preexisting bridge archive was accepted")
    assert_true(
        file_inventory(pathlib.Path(changed_fixture["oni"])) == before_changed_refusal,
        "changed archive refusal changed the fixture",
    )
    return {
        "preexisting_archive_snapshot": "recursive_path_hash_size_mtime",
        "bootstrap_added_old_archive": "only_allowed_delta",
        "unexpected_extra_entry": "infra_fail_refused",
        "changed_preexisting_entry": "infra_fail_refused",
    }


def test_path_and_root_guards(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "path-escape", run_id="g0-path-escape")
    outside = pathlib.Path(fixture["case"]) / "outside-loader.sav"
    write_bytes(outside, b"outside")
    arguments = [sys.executable, str(CORE), "prepare", *common_args(fixture)]
    loader_index = arguments.index("--loader-save") + 1
    arguments[loader_index] = str(outside)
    blocked = json.loads(run(arguments, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "save path escape was accepted")
    assert_true(not fixture_paths(fixture)["environment"].exists(), "path refusal wrote evidence")
    try:
        workflow.assert_safe_root(pathlib.Path(pathlib.Path(fixture["case"]).anchor), "drive root")
        raise VerificationFailure("filesystem root was accepted")
    except workflow.WorkflowError:
        pass
    try:
        workflow.assert_safe_root(pathlib.Path(r"\\fixture-server\fixture-share"), "UNC root")
        raise VerificationFailure("UNC path was accepted")
    except workflow.WorkflowError:
        pass
    missing_boundary = pathlib.Path(fixture["case"]) / "missing-parent-boundary"
    missing_boundary.mkdir()
    try:
        workflow.assert_no_reparse(
            missing_boundary / "missing-parent" / "leaf.json",
            missing_boundary,
            allow_missing_leaf=True,
        )
        raise VerificationFailure("allow-missing-leaf accepted a missing ancestor")
    except workflow.WorkflowError:
        missing_parent = "blocked"
    return {
        "save_escape": "blocked",
        "filesystem_root": "blocked",
        "unc_root": "blocked",
        "missing_intermediate_parent": missing_parent,
    }


def test_process_command_line_false_positive_guards(base: pathlib.Path) -> dict[str, Any]:
    del base
    records = [
        {
            "Name": "pwsh.exe",
            "CommandLine": "pwsh -Command Where-Object {$_.CommandLine -like '*oni_bridge.py*'}",
        },
        {"Name": "python.exe", "CommandLine": 'python.exe -c "print(\'oni_bridge.py\')"'},
        {"Name": "python.exe", "CommandLine": "python.exe C:\\tools\\worker.py --label oni_bridge.py"},
        {"Name": "python.exe", "CommandLine": "python.exe C:\\tools\\worker --label oni_bridge.py"},
        {"Name": "python.exe", "CommandLine": "python.exe -m fixture.worker oni_bridge.py"},
        {"Name": "python.exe", "CommandLine": "python.exe C:\\tools\\oni_bridge.py.bak"},
        {"Name": "python.exe", "CommandLine": "python.exe C:\\tools\\oni_bridge.py_helper.py"},
        {"Name": "python.exe", "CommandLine": 'python.exe -u "C:\\runtime\\oni_bridge.py" --once'},
        {"Name": "pythonw.exe", "CommandLine": "pythonw.exe C:/runtime/ONI_BRIDGE.PY --quiet"},
        {"Name": "py.exe", "CommandLine": "py.exe -3.12 -X utf8 C:\\runtime\\oni_bridge.py"},
    ]
    count = workflow.count_bridge_process_records(records)
    assert_true(count == 3, f"strict bridge argv classifier counted {count}, expected 3")
    return {
        "candidate_names": ["python.exe", "pythonw.exe", "py.exe"],
        "powershell_self_match": "ignored_by_process_name",
        "python_c_string_false_positive": "ignored",
        "later_data_argument_false_positive": "ignored_after_first_py_script",
        "suffix_false_positives": "ignored",
        "exact_oni_bridge_scripts": 3,
    }


def test_concurrent_source_change(base: pathlib.Path) -> dict[str, Any]:
    case = base / "concurrent-copy"
    source = case / "source.bin"
    target = case / "target.bin"
    write_bytes(source, b"before")

    def mutate() -> None:
        write_bytes(source, b"changed-concurrently")

    try:
        workflow.copy_file_stable(source, target, before_commit_hook=mutate)
        raise VerificationFailure("concurrent file change was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(not target.exists(), "concurrency refusal committed a target")
    assert_true(not list(case.glob("*.tmp")), "concurrency refusal left a temporary file")

    late_file_case = base / "copy-file-late-target"
    late_file_source = late_file_case / "source.bin"
    late_file_target = late_file_case / "target.bin"
    late_file_payload = b"external-late-file-target\n"
    write_bytes(late_file_source, b"owned-source\n")

    def create_late_file_target() -> None:
        write_bytes(late_file_target, late_file_payload)

    try:
        workflow.copy_file_stable(
            late_file_source, late_file_target, before_commit_hook=create_late_file_target
        )
        raise VerificationFailure("late copy-file target was overwritten")
    except workflow.WorkflowError:
        pass
    assert_true(late_file_target.read_bytes() == late_file_payload, "late file target changed")
    assert_true(late_file_source.read_bytes() == b"owned-source\n", "copy source changed on collision")

    late_tree_case = base / "copy-tree-late-target"
    late_tree_source = late_tree_case / "source"
    late_tree_target = late_tree_case / "target"
    write_bytes(late_tree_source / "owned.txt", b"owned-tree-source\n")
    late_tree_payload = b"external-late-tree-target\n"

    def create_late_tree_target() -> None:
        write_bytes(late_tree_target / "external.txt", late_tree_payload)

    try:
        workflow.copy_tree_exact(
            late_tree_source,
            late_tree_target,
            workflow.snapshot_tree(late_tree_source),
            before_commit_hook=create_late_tree_target,
        )
        raise VerificationFailure("late copy-tree target was overwritten")
    except workflow.WorkflowError:
        pass
    assert_true(
        (late_tree_target / "external.txt").read_bytes() == late_tree_payload,
        "late tree target changed",
    )

    late_move_case = base / "move-late-target"
    late_move_source = late_move_case / "source"
    late_move_target = late_move_case / "target"
    late_move_payload = b"external-late-move-target\n"
    write_bytes(late_move_source, b"owned-move-source\n")

    def create_late_move_target(_label: str, target_path: pathlib.Path) -> None:
        write_bytes(target_path, late_move_payload)

    late_move_tx = workflow.FileTransaction(
        late_move_case / "journal.jsonl",
        late_move_case / "recovery",
        before_publish_hook=create_late_move_target,
    )
    try:
        late_move_tx.move(late_move_source, late_move_target, "late_move")
        raise VerificationFailure("late move target was overwritten")
    except workflow.WorkflowError:
        pass
    assert_true(late_move_target.read_bytes() == late_move_payload, "late move target changed")
    assert_true(late_move_source.read_bytes() == b"owned-move-source\n", "move source was lost")

    file_stage_case = base / "post-rename-file-stage"
    file_source = file_stage_case / "source.bin"
    file_target = file_stage_case / "target.bin"
    write_bytes(file_source, b"owned-copy\n")
    recreated_files: list[pathlib.Path] = []

    def recreate_file_stage(_target: pathlib.Path, temporary: pathlib.Path) -> None:
        write_bytes(temporary, b"external-recreated-file-stage\n")
        recreated_files.append(temporary)

    workflow.copy_file_stable(file_source, file_target, after_publish_hook=recreate_file_stage)
    assert_true(
        len(recreated_files) == 1 and recreated_files[0].read_bytes() == b"external-recreated-file-stage\n",
        "post-rename file staging replacement was deleted",
    )

    tree_stage_case = base / "post-rename-tree-stage"
    tree_source = tree_stage_case / "source"
    tree_target = tree_stage_case / "target"
    write_bytes(tree_source / "owned.txt", b"owned-tree\n")
    tree_expected = workflow.snapshot_tree(tree_source)
    recreated_trees: list[pathlib.Path] = []

    def recreate_tree_stage(_target: pathlib.Path, staging: pathlib.Path) -> None:
        write_bytes(staging / "external.txt", b"external-recreated-tree-stage\n")
        recreated_trees.append(staging)

    workflow.copy_tree_exact(
        tree_source,
        tree_target,
        tree_expected,
        after_publish_hook=recreate_tree_stage,
    )
    assert_true(
        len(recreated_trees) == 1
        and (recreated_trees[0] / "external.txt").read_bytes() == b"external-recreated-tree-stage\n",
        "post-rename tree staging replacement was recursively deleted",
    )
    return {
        "classification": "infra_fail",
        "concurrent_commit": "refused",
        "late_target_file_copy": "create_only_refused",
        "late_target_tree_copy": "create_only_refused",
        "late_target_move": "create_only_refused",
        "temporary_files": 0,
        "post_rename_file_stage_replacement": "retained",
        "post_rename_tree_stage_replacement": "retained",
    }


def test_post_boundary_receipt_and_save_mtime_guards(
    base: pathlib.Path,
) -> dict[str, Any]:
    results: dict[str, str] = {}

    def run_attack(
        index: int,
        action: str,
        *,
        target_key: str | None = None,
    ) -> tuple[str, bool]:
        token = f"pr-{index}"
        fixture = make_fixture(
            base,
            token,
            run_id=f"g0-{token}",
            with_extra_save_pair=True,
        )
        prepare_fixture(fixture)
        simulate_runtime(fixture)
        apply_args = workflow_finalize_namespace(fixture, apply=True)
        roots = workflow.resolve_roots(apply_args)
        final_root = roots.environment_dir / "finalization"
        recovery_path = final_root / "finalize-recovery.json"
        result_path = final_root / "finalize-result.json"
        target = pathlib.Path(fixture[target_key]) if target_key is not None else None
        target_bytes = target.read_bytes() if target is not None else None
        target_before = target.stat() if target is not None else None
        receipt_intercepted = False
        receipt_path_id_verified = False
        target_mtime_changed = False
        original_post_boundary = workflow.run_post_boundary
        original_subprocess_run = subprocess.run

        def intercept_capture(
            command: Any,
            *run_args: Any,
            **run_kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal receipt_intercepted, receipt_path_id_verified
            completed = original_subprocess_run(command, *run_args, **run_kwargs)
            command_parts = (
                [str(item) for item in command]
                if isinstance(command, (list, tuple))
                else []
            )
            if (
                completed.returncode != 0
                or str(CAPTURE).casefold() not in {item.casefold() for item in command_parts}
            ):
                return completed
            try:
                report_root = pathlib.Path(command_parts[command_parts.index("-ReportRoot") + 1])
                run_id = command_parts[command_parts.index("-RunId") + 1]
            except (ValueError, IndexError) as exc:
                raise VerificationFailure(
                    "capture command omitted its exact report target"
                ) from exc
            report = report_root / run_id / "post-boundary.json"
            receipt = json.loads(completed.stdout.lstrip("\ufeff"))
            if not isinstance(receipt, dict):
                raise VerificationFailure("capture receipt attack fixture received a non-object")
            receipt_intercepted = True
            receipt_path_id_verified = (
                receipt.get("report_path_id")
                == workflow.path_id(report, contract="lower")
            )
            if not receipt_path_id_verified:
                raise VerificationFailure(
                    "PowerShell and manager report path-id normalization contracts differ"
                )

            rewrite_receipt = False
            if action == "receipt_extra":
                receipt["unexpected"] = True
                rewrite_receipt = True
            elif action == "receipt_missing":
                receipt.pop("report_sha256", None)
                rewrite_receipt = True
            elif action == "receipt_wrong_path_id":
                receipt["report_path_id"] = "sha256:" + "0" * 64
                rewrite_receipt = True
            elif action == "same_bytes_name_swap":
                owner = report.with_name("post-boundary.writer-owned.json")
                os.replace(report, owner)
                shutil.copy2(owner, report)
            elif action == "hardlink":
                os.link(report, report.with_name("post-boundary.alias.json"))
            elif action == "parent_reparse":
                original_parent = report.parent
                owner = original_parent.with_name(original_parent.name + "-owner")
                os.replace(original_parent, owner)
                junction_command = (
                    "& { param([string]$Link,[string]$Target) "
                    "New-Item -ItemType Junction -Path $Link -Target $Target "
                    "-ErrorAction Stop | Out-Null }"
                )
                junction = original_subprocess_run(
                    [
                        POWERSHELL,
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        junction_command,
                        str(original_parent),
                        str(owner),
                    ],
                    cwd=ROOT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                if junction.returncode != 0:
                    raise VerificationFailure(
                        "could not create the TestRoot report-parent reparse fixture"
                    )
            elif action in {
                "forged_report_original_receipt",
                "forged_report_self_consistent_receipt",
            }:
                if target is None:
                    raise VerificationFailure("forged report attack omitted its target save")
                pre_path = (
                    pathlib.Path(fixture["boundaries"])
                    / str(fixture["run_id"])
                    / "pre-boundary.json"
                )
                pre_document = json.loads(pre_path.read_text(encoding="utf-8"))
                post_document = json.loads(report.read_text(encoding="utf-8"))
                target_id = workflow.path_id(target, contract="lower")
                pre_entries = {
                    str(item["path_id"]): item
                    for item in pre_document["saves"]["entries"]
                }
                replaced = False
                forged_entries: list[dict[str, Any]] = []
                for item in post_document["saves"]["entries"]:
                    if item.get("path_id") == target_id:
                        forged_entries.append(copy.deepcopy(pre_entries[target_id]))
                        replaced = True
                    else:
                        forged_entries.append(item)
                if not replaced:
                    raise VerificationFailure("mtime attack target was absent from the post report")
                post_document["saves"]["entries"] = forged_entries
                write_json(report, post_document)
                if action == "forged_report_self_consistent_receipt":
                    metadata = os.lstat(report)
                    parent_metadata = os.lstat(report.parent)
                    receipt["report_sha256"] = sha256(report)
                    receipt["report_byte_length"] = metadata.st_size
                    receipt["report_identity"]["file_index"] = metadata.st_ino
                    receipt["report_identity"]["number_of_links"] = metadata.st_nlink
                    receipt["report_parent_identity"]["file_index"] = parent_metadata.st_ino
                    rewrite_receipt = True

            if rewrite_receipt:
                return subprocess.CompletedProcess(
                    completed.args,
                    completed.returncode,
                    stdout=json.dumps(receipt, ensure_ascii=False),
                    stderr=completed.stderr,
                )
            return completed

        def attacked_post_boundary(
            call_args: argparse.Namespace,
            call_roots: workflow.Roots,
            call_final_root: pathlib.Path,
            manifest: Mapping[str, Any],
            *post_args: Any,
            **post_kwargs: Any,
        ) -> dict[str, Any]:
            nonlocal target_mtime_changed
            if target is not None and not target_mtime_changed:
                metadata = target.stat()
                os.utime(
                    target,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 10_000_000_000),
                )
                target_mtime_changed = True
            workflow.subprocess.run = intercept_capture
            try:
                return original_post_boundary(
                    call_args,
                    call_roots,
                    call_final_root,
                    manifest,
                    *post_args,
                    **post_kwargs,
                )
            finally:
                workflow.subprocess.run = original_subprocess_run

        workflow.run_post_boundary = attacked_post_boundary
        blocker = ""
        try:
            try:
                with workflow.exclusive_finalize_lock(roots):
                    workflow.finalize_locked(apply_args, roots)
                raise VerificationFailure(f"{action} attack unexpectedly finalized")
            except workflow.WorkflowError as exc:
                blocker = str(exc)
        finally:
            workflow.run_post_boundary = original_post_boundary
            workflow.subprocess.run = original_subprocess_run

        assert_true(receipt_intercepted, f"{action} did not intercept the post capture")
        assert_true(
            receipt_path_id_verified,
            f"{action} did not verify the exact writer/manager target path id",
        )
        assert_true(recovery_path.is_file(), f"{action} did not preserve recovery evidence")
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        assert_true(
            recovery.get("state")
            == "post_boundary_or_publication_blocked_after_physical_commit"
            and recovery.get("candidate_result") is None
            and not result_path.exists(),
            f"{action} did not remain blocked without a final result",
        )
        journal = workflow.verify_journal(roots.environment_dir / "restore-journal.jsonl")
        assert_true(
            sum(
                item.get("event") == "restore_transaction_physical_committed"
                for item in journal
            )
            == 1,
            f"{action} crossed the physical commit boundary more than once",
        )
        if target is not None:
            assert_true(
                target.read_bytes() == target_bytes
                and target.stat().st_mtime_ns != target_before.st_mtime_ns,
                f"{action} did not preserve the intended same-byte mtime-only attack",
            )
        return blocker, receipt_path_id_verified

    for index, action in enumerate(
        (
            "receipt_extra",
            "receipt_missing",
            "receipt_wrong_path_id",
            "same_bytes_name_swap",
            "hardlink",
            "parent_reparse",
        )
    ):
        run_attack(index, action)
        results[action] = "refused_with_nonreplay_recovery"

    original_blocker, _ = run_attack(
        6,
        "forged_report_original_receipt",
        target_key="extra_save",
    )
    assert_true(
        "receipt/report binding" in original_blocker,
        "post-return forged report was not refused by the raw receipt binding",
    )
    results["mtime_forged_report_original_receipt"] = "raw_receipt_refused"

    for index, target_key in ((7, "extra_save"), (8, "extra_preview")):
        blocker, _ = run_attack(
            index,
            "forged_report_self_consistent_receipt",
            target_key=target_key,
        )
        assert_true(
            "current save set differs" in blocker,
            f"self-consistent forged {target_key} report bypassed final mtime custody",
        )
        results[f"mtime_{target_key}_self_consistent_receipt"] = (
            "final_save_custody_refused"
        )

    results["receipt_path_id_normalization"] = "powershell_manager_lower_exact"
    return results


def test_windows_extended_path_tree_snapshot(base: pathlib.Path) -> dict[str, Any]:
    if os.name != "nt":
        return {"platform": "non_windows", "coverage": "not_applicable"}

    def test_extended_path(path: pathlib.Path) -> str:
        text = os.path.abspath(os.path.normpath(os.fspath(path)))
        return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text

    case = base / "long-path-snapshot"
    archive_root = case / "ai-bridge-archives"
    archive_root.mkdir(parents=True)
    long_file = (
        archive_root
        / ("archive-" + "a" * 112)
        / ("metadata-" + "b" * 112)
        / "environment-metadata.json"
    )
    payload = b'{"archive":"long-path"}\n'
    logical_file = workflow.normalized(long_file)
    logical_units = len(str(logical_file).encode("utf-16-le")) // 2
    assert_true(logical_units > 260, "long-path regression did not cross MAX_PATH")
    assert_true(
        not str(logical_file).startswith("\\\\?\\")
        and workflow._io_path(logical_file).startswith("\\\\?\\"),
        "Windows extended path was not confined to the OS-I/O adapter",
    )

    descriptor: int | None = None
    real_lstat = workflow.os.lstat
    real_open = workflow.os.open
    real_scandir = workflow.os.scandir
    real_stat = workflow.os.stat
    observed = {"lstat_extended": 0, "open_extended": 0, "scandir_extended": 0, "stat_extended": 0}
    hooks: list[pathlib.Path] = []

    def is_unprefixed_long_path(path: Any) -> bool:
        text = os.fspath(path)
        return (
            isinstance(text, str)
            and len(text.encode("utf-16-le")) // 2 > 260
            and not text.startswith("\\\\?\\")
        )

    def guarded_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        text = os.fspath(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy path refusal", text)
        if isinstance(text, str) and text.startswith("\\\\?\\"):
            observed["lstat_extended"] += 1
        return real_lstat(path, *args, **kwargs)

    def guarded_open(path: Any, *args: Any, **kwargs: Any) -> int:
        text = os.fspath(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy path refusal", text)
        if isinstance(text, str) and text.startswith("\\\\?\\"):
            observed["open_extended"] += 1
        return real_open(path, *args, **kwargs)

    def guarded_scandir(path: Any) -> Any:
        text = os.fspath(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy traversal refusal", text)
        if isinstance(text, str) and text.startswith("\\\\?\\"):
            observed["scandir_extended"] += 1
        return real_scandir(path)

    def guarded_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        text = os.fspath(path)
        if is_unprefixed_long_path(path):
            raise FileNotFoundError(2, "injected legacy stat refusal", text)
        if isinstance(text, str) and text.startswith("\\\\?\\"):
            observed["stat_extended"] += 1
        return real_stat(path, *args, **kwargs)

    try:
        os.makedirs(test_extended_path(logical_file.parent))
        descriptor = real_open(
            test_extended_path(logical_file),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.write(descriptor, payload)
        os.close(descriptor)
        descriptor = None

        workflow.os.lstat = guarded_lstat
        workflow.os.open = guarded_open
        workflow.os.scandir = guarded_scandir
        workflow.os.stat = guarded_stat
        assert_true(not workflow.is_reparse(logical_file), "ordinary long-path file looked reparsed")
        stable = workflow._stable_file_snapshot(
            logical_file,
            collect_bytes=True,
            after_lstat_hook=hooks.append,
            after_read_hook=hooks.append,
        )
        tree = workflow.snapshot_tree(archive_root)
    finally:
        workflow.os.lstat = real_lstat
        workflow.os.open = real_open
        workflow.os.scandir = real_scandir
        workflow.os.stat = real_stat
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(test_extended_path(case)):
            shutil.rmtree(test_extended_path(case))

    relative = logical_file.relative_to(workflow.normalized(archive_root)).as_posix()
    file_entries = [entry for entry in tree["entries"] if entry["kind"] == "file"]
    expected_path_id = workflow.sha256_bytes(str(logical_file).lower().encode("utf-8"))
    assert_true(
        stable["bytes"] == payload
        and stable["sha256"] == workflow.sha256_bytes(payload)
        and len(file_entries) == 1
        and file_entries[0]["relative"] == relative
        and file_entries[0]["sha256"] == workflow.sha256_bytes(payload),
        "long-path tree snapshot did not preserve the logical archive metadata",
    )
    assert_true(
        hooks == [logical_file, logical_file]
        and all(not str(path).startswith("\\\\?\\") for path in hooks)
        and workflow.path_id(logical_file, contract="lower") == expected_path_id,
        "extended prefix leaked into hooks or the path-id contract",
    )
    assert_true(
        observed["lstat_extended"] >= 4
        and observed["open_extended"] >= 2
        and observed["scandir_extended"] >= 1,
        "long-path lstat/open/traversal did not consistently use the extended path adapter",
    )
    return {
        "platform": "windows",
        "logical_utf16_units": logical_units,
        "relative_path_preserved": True,
        "path_id_contract_preserved": True,
        "lstat_extended_calls": observed["lstat_extended"],
        "open_extended_calls": observed["open_extended"],
        "scandir_extended_calls": observed["scandir_extended"],
        "stat_extended_calls": observed["stat_extended"],
    }


def test_stable_descriptor_evidence_reads(base: pathlib.Path) -> dict[str, Any]:
    case = base / "stable-descriptor-reads"
    hardlink = case / "hardlink.json"
    alias = case / "hardlink-alias.json"
    write_json(hardlink, {"value": "trusted"})
    os.link(hardlink, alias)
    try:
        workflow.load_json(hardlink, "hardlinked fixture")
        raise VerificationFailure("hardlinked security JSON was accepted")
    except workflow.WorkflowError:
        hardlink_result = "single_link_refused"

    original = case / "name-swap.json"
    moved = case / "name-swap-owned.json"
    replacement = case / "name-swap-replacement.json"
    write_json(original, {"value": "trusted"})
    write_json(replacement, {"value": "external"})
    replacement_bytes = replacement.read_bytes()

    def swap_after_lstat(path: pathlib.Path) -> None:
        os.replace(path, moved)
        os.replace(replacement, path)

    try:
        workflow.load_json(original, "name-swap fixture", after_lstat_hook=swap_after_lstat)
        raise VerificationFailure("verify-to-open JSON name swap was accepted")
    except workflow.WorkflowError:
        name_swap = "descriptor_identity_refused"
    assert_true(original.read_bytes() == replacement_bytes and moved.exists(), "JSON replacement changed")

    drift = case / "in-place-drift.json"
    write_json(drift, {"value": "trusted-a"})
    drift_stamp = drift.stat().st_mtime_ns

    def mutate_after_read(path: pathlib.Path) -> None:
        write_json(path, {"value": "attack--b"})
        os.utime(path, ns=(drift_stamp, drift_stamp))

    try:
        workflow.load_json(drift, "in-place drift fixture", after_read_hook=mutate_after_read)
        raise VerificationFailure("same-name in-place JSON drift was accepted")
    except workflow.WorkflowError:
        in_place = "same_descriptor_second_read_hash_refused"
    return {
        "hardlink": hardlink_result,
        "verify_to_open_name_swap": name_swap,
        "same_name_same_mtime_in_place_drift": in_place,
        "primitive": "single_link_descriptor_path_identity_raw_hash",
    }


def test_transaction_rollback_ownership_refusals(base: pathlib.Path) -> dict[str, Any]:
    results: dict[str, str] = {}

    def exercise(kind: str) -> None:
        case = base / f"rollback-ownership-{kind}"
        recovery = case / "recovery"
        recovery.mkdir(parents=True)
        journal = case / "journal.jsonl"
        target = case / "target"
        moved_owner = case / "owned-output-moved"
        replacement = case / "external-replacement"
        replacement_payload = b"external-replacement-must-survive\n"
        if kind == "copy_tree":
            replacement.mkdir()
            write_bytes(replacement / "external.txt", replacement_payload)
        else:
            write_bytes(replacement, replacement_payload)

        def replace_after_step(_step: int, _label: str) -> None:
            os.replace(target, moved_owner)
            os.replace(replacement, target)

        tx = workflow.FileTransaction(
            journal,
            recovery,
            fault_after=1,
            after_physical_hook=lambda label, _target: replace_after_step(1, label),
        )
        try:
            if kind == "move":
                source = case / "source"
                write_bytes(source, b"owned-move-source\n")
                tx.move(source, target, "move")
            elif kind == "copy_file":
                source = case / "source"
                write_bytes(source, b"owned-copy-source\n")
                tx.copy_file(source, target, "copy_file")
            elif kind == "copy_tree":
                source = case / "source-tree"
                write_bytes(source / "owned.txt", b"owned-tree-source\n")
                expected = workflow.snapshot_tree(source)
                tx.copy_tree(source, target, expected, "copy_tree")
            elif kind == "set_mtime":
                write_bytes(target, b"owned-mtime-source\n", stamp=1_725_000_000_000_000_000)
                tx.set_mtime(target, 1_725_000_001_000_000_000, "set_mtime")
            else:
                raise VerificationFailure("unknown rollback ownership fixture")
            raise VerificationFailure(f"{kind} fault injection did not fire")
        except workflow.WorkflowError:
            pass
        try:
            tx.rollback()
            raise VerificationFailure(f"{kind} rollback accepted an external replacement")
        except workflow.WorkflowError:
            results[kind] = "outcome_unknown_external_state_retained"

        if kind == "copy_tree":
            assert_true((target / "external.txt").read_bytes() == replacement_payload, f"{kind} replacement changed")
        else:
            assert_true(target.read_bytes() == replacement_payload, f"{kind} replacement changed")
        assert_true(moved_owner.exists(), f"{kind} owned output was not preserved for recovery")
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        assert_true(
            any(
                entry.get("event") == "restore_rollback_step"
                and entry.get("data", {}).get("status") == "outcome_unknown_external_state_retained"
                and entry.get("data", {}).get("recovery_required") is True
                for entry in entries
            ),
            f"{kind} rollback did not journal recovery-required outcome_unknown",
        )

    for kind in ("move", "copy_file", "copy_tree", "set_mtime"):
        exercise(kind)

    late_results: dict[str, str] = {}

    def exercise_late_claim(kind: str) -> None:
        case = base / f"rollback-late-claim-{kind}"
        recovery = case / "recovery"
        recovery.mkdir(parents=True)
        journal = case / "journal.jsonl"
        target = case / "target"
        moved_owner = case / "owned-output-moved"
        external = case / "external-replacement"
        external_payload = b"late-external-replacement\n"
        if kind == "copy_tree":
            write_bytes(external / "external.txt", external_payload)
        else:
            write_bytes(external, external_payload)

        def swap_after_check_boundary(_label: str, path: pathlib.Path) -> None:
            os.rename(path, moved_owner)
            os.rename(external, path)

        tx = workflow.FileTransaction(
            journal,
            recovery,
            fault_after=1,
            before_undo_claim_hook=swap_after_check_boundary,
        )
        try:
            if kind == "move":
                source = case / "source"
                write_bytes(source, b"owned-move\n")
                tx.move(source, target, "move")
            elif kind == "copy_file":
                source = case / "source"
                write_bytes(source, b"owned-copy\n")
                tx.copy_file(source, target, "copy_file")
            elif kind == "copy_tree":
                source = case / "source-tree"
                write_bytes(source / "owned.txt", b"owned-tree\n")
                tx.copy_tree(source, target, workflow.snapshot_tree(source), "copy_tree")
            else:
                write_bytes(target, b"owned-mtime\n", stamp=1_725_000_000_000_000_000)
                tx.set_mtime(target, 1_725_000_001_000_000_000, "set_mtime")
        except workflow.WorkflowError:
            pass
        try:
            tx.rollback()
            raise VerificationFailure(f"late {kind} swap was accepted")
        except workflow.WorkflowError:
            pass
        if kind == "copy_tree":
            assert_true((target / "external.txt").read_bytes() == external_payload, f"late {kind} replacement moved")
        else:
            assert_true(target.read_bytes() == external_payload, f"late {kind} replacement changed")
        assert_true(moved_owner.exists(), f"late {kind} owned output recovery was lost")
        late_results[kind] = "atomic_claim_mismatch_external_path_restored"

    for kind in ("move", "copy_file", "copy_tree", "set_mtime"):
        exercise_late_claim(kind)

    original_mtime = 1_725_000_000_000_000_000
    changed_mtime = 1_725_000_001_000_000_000
    mtime_window_results: dict[str, str] = {}
    for node_kind in ("file", "directory"):
        mtime_window = base / f"rollback-mtime-after-physical-before-verify-{node_kind}"
        mtime_recovery = mtime_window / "recovery"
        mtime_recovery.mkdir(parents=True)
        mtime_journal = mtime_window / "journal.jsonl"
        mtime_target = mtime_window / "target"
        if node_kind == "file":
            write_bytes(mtime_target, b"owned-mtime-window\n", stamp=original_mtime)
        else:
            write_bytes(mtime_target / "owned.txt", b"owned-mtime-tree\n")
            os.utime(mtime_target, ns=(original_mtime, original_mtime))
        before_mtime_state = workflow.capture_transaction_node(mtime_target)
        fired = False

        def fail_after_mtime_physical(label: str, target: pathlib.Path) -> None:
            nonlocal fired
            if label == "set_mtime_window" and target == workflow.normalized(mtime_target):
                fired = True
                raise workflow.WorkflowError("injected after_mtime_physical_before_undo fault")

        mtime_tx = workflow.FileTransaction(
            mtime_journal,
            mtime_recovery,
            after_physical_hook=fail_after_mtime_physical,
        )
        try:
            mtime_tx.set_mtime(mtime_target, changed_mtime, "set_mtime_window")
            raise VerificationFailure("after-mtime physical fault did not fire")
        except workflow.WorkflowError:
            pass
        mtime_tx.rollback()
        assert_true(
            fired
            and workflow.capture_transaction_node(mtime_target) == before_mtime_state
            and not (mtime_recovery / "transaction-recovery.json").exists(),
            f"after-mtime physical fault did not restore exact {node_kind} prestate",
        )
        mtime_window_results[node_kind] = "exact_prestate_restored"
    return {
        "operations": results,
        "after_check_before_undo": late_results,
        "after_mtime_physical_before_undo": mtime_window_results,
        "external_replacements_retained": True,
        "recovery_required": True,
    }


def test_transaction_journal_fault_state_machine(base: pathlib.Path) -> dict[str, Any]:
    results: dict[str, str] = {}

    def writer_for(target_event: str, target_stage: str) -> Callable[[pathlib.Path, str, dict[str, Any]], dict[str, Any]]:
        fired = False

        def writer(path: pathlib.Path, event: str, data: dict[str, Any]) -> dict[str, Any]:
            nonlocal fired

            def fault(stage: str) -> None:
                nonlocal fired
                if not fired and event == target_event and stage == target_stage:
                    fired = True
                    raise RuntimeError(f"injected {event} {stage}")

            return workflow.append_journal(path, event, data, fault_hook=fault)

        return writer

    intent_case = base / "journal-fault-intent"
    intent_case.mkdir()
    intent_source = intent_case / "source"
    intent_target = intent_case / "target"
    write_bytes(intent_source, b"intent-source\n")
    intent_tx = workflow.FileTransaction(
        intent_case / "journal.jsonl",
        intent_case / "recovery",
        journal_append=writer_for("restore_step_intent", "after_write"),
    )
    try:
        intent_tx.move(intent_source, intent_target, "intent")
        raise VerificationFailure("intent journal fault did not block physical move")
    except workflow.WorkflowError:
        pass
    assert_true(intent_source.exists() and not intent_target.exists(), "intent fault crossed physical boundary")
    workflow.verify_journal(intent_case / "journal.jsonl")
    results["intent_after_write"] = "truncated_before_physical_action"

    completed_case = base / "journal-fault-completed"
    completed_case.mkdir()
    completed_source = completed_case / "source"
    completed_target = completed_case / "target"
    write_bytes(completed_source, b"completed-source\n")
    completed_tx = workflow.FileTransaction(
        completed_case / "journal.jsonl",
        completed_case / "recovery",
        journal_append=writer_for("restore_step_completed", "after_write"),
    )
    try:
        completed_tx.move(completed_source, completed_target, "completed")
        raise VerificationFailure("completed journal fault did not surface")
    except workflow.WorkflowError:
        pass
    completed_tx.rollback()
    assert_true(completed_source.exists() and not completed_target.exists(), "completed fault did not rollback")
    workflow.verify_journal(completed_case / "journal.jsonl")
    results["completed_after_write"] = "physical_action_rolled_back"

    rollback_case = base / "journal-fault-rollback"
    rollback_case.mkdir()
    rollback_source = rollback_case / "source"
    rollback_target = rollback_case / "target"
    write_bytes(rollback_source, b"rollback-source\n")
    rollback_tx = workflow.FileTransaction(
        rollback_case / "journal.jsonl",
        rollback_case / "recovery",
        fault_after=1,
        journal_append=writer_for("restore_rollback_started", "after_write"),
    )
    try:
        rollback_tx.move(rollback_source, rollback_target, "rollback")
    except workflow.WorkflowError:
        pass
    try:
        rollback_tx.rollback()
        raise VerificationFailure("rollback journal failure was not classified outcome_unknown")
    except workflow.WorkflowError:
        pass
    assert_true(rollback_source.exists() and not rollback_target.exists(), "rollback journal fault stopped in-memory undo")
    assert_true((rollback_case / "recovery" / "transaction-recovery.json").exists(), "rollback fault lacked recovery evidence")
    workflow.verify_journal(rollback_case / "journal.jsonl")
    results["rollback_started_after_write"] = "best_effort_undo_completed_recovery_required"

    for target_event, target_stage in (
        ("restore_rollback_started", "before_write"),
        ("restore_rollback_step", "after_fsync"),
        ("restore_rollback_complete", "after_write"),
    ):
        label = f"{target_event.removeprefix('restore_')}_{target_stage}"
        case = base / f"journal-fault-{label}"
        case.mkdir()
        source = case / "source"
        target = case / "target"
        write_bytes(source, f"{label}\n".encode("utf-8"))
        tx = workflow.FileTransaction(
            case / "journal.jsonl",
            case / "recovery",
            fault_after=1,
            journal_append=writer_for(target_event, target_stage),
        )
        try:
            tx.move(source, target, label)
        except workflow.WorkflowError:
            pass
        try:
            tx.rollback()
            raise VerificationFailure(f"{label} did not retain an outcome_unknown latch")
        except workflow.WorkflowError:
            pass
        assert_true(source.exists() and not target.exists(), f"{label} stopped physical undo")
        assert_true(
            (case / "recovery" / "transaction-recovery.json").is_file(),
            f"{label} did not publish transaction recovery evidence",
        )
        workflow.verify_journal(case / "journal.jsonl")
        results[label] = "physical_undo_complete_replay_latched"

    commit_case = base / "journal-fault-commit"
    commit_case.mkdir()
    commit_source = commit_case / "source"
    commit_target = commit_case / "target"
    write_bytes(commit_source, b"commit-source\n")
    commit_tx = workflow.FileTransaction(
        commit_case / "journal.jsonl",
        commit_case / "recovery",
        journal_append=writer_for("restore_transaction_physical_committed", "after_fsync"),
    )
    commit_tx.move(commit_source, commit_target, "commit")
    try:
        commit_tx.commit()
        raise VerificationFailure("commit after-fsync fault was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(not commit_tx.committed and commit_tx.undos, "commit fault discarded rollback ownership")
    commit_tx.rollback()
    assert_true(commit_source.exists() and not commit_target.exists(), "commit journal fault was not rolled back")
    workflow.verify_journal(commit_case / "journal.jsonl")
    results["commit_after_fsync"] = "journal_truncated_and_physical_action_rolled_back"

    swap_case = base / "journal-name-swap"
    swap_case.mkdir()
    swap_journal = swap_case / "journal.jsonl"
    workflow.append_journal(swap_journal, "fixture_initial", {"value": 1})
    original_bytes = swap_journal.read_bytes()
    replacement = swap_case / "replacement.jsonl"
    moved_owner = swap_case / "owned-journal-moved.jsonl"
    write_bytes(replacement, original_bytes)

    def swap_before_open() -> None:
        os.replace(swap_journal, moved_owner)
        os.replace(replacement, swap_journal)

    try:
        workflow.append_journal(
            swap_journal,
            "must_not_append",
            {"value": 2},
            before_open_hook=swap_before_open,
        )
        raise VerificationFailure("journal name swap was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(swap_journal.read_bytes() == original_bytes, "external journal replacement was written or truncated")
    results["verify_to_open_name_swap"] = "replacement_retained_unmodified"

    hardlink_case = base / "journal-hardlink"
    hardlink_case.mkdir()
    hardlink_journal = hardlink_case / "journal.jsonl"
    hardlink_alias = hardlink_case / "journal-alias.jsonl"
    workflow.append_journal(hardlink_journal, "fixture_initial", {"value": 1})
    os.link(hardlink_journal, hardlink_alias)
    try:
        workflow.verify_journal(hardlink_journal)
        raise VerificationFailure("hardlinked journal was accepted")
    except workflow.WorkflowError:
        results["hardlink"] = "descriptor_snapshot_refused"

    empty_case = base / "journal-missing-foreign-empty"
    empty_case.mkdir()
    empty_journal = empty_case / "journal.jsonl"

    def create_foreign_empty() -> None:
        write_bytes(empty_journal, b"")

    try:
        workflow.append_journal(
            empty_journal,
            "must_not_append",
            {"value": 3},
            before_open_hook=create_foreign_empty,
        )
        raise VerificationFailure("foreign empty journal won create-exclusive race")
    except workflow.WorkflowError:
        pass
    assert_true(empty_journal.read_bytes() == b"", "foreign empty journal was written")
    results["missing_to_foreign_empty"] = "create_exclusive_refused_unmodified"
    return {"faults": results, "journal_chain_after_faults": "valid", "state_machine": "explicit"}


def test_finalize_transaction_epoch_reducer_and_latch(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "finalize-epoch-reducer", run_id="g0-finalize-epoch-reducer")
    prepare_fixture(fixture)
    simulate_runtime(fixture)
    before = file_inventory(pathlib.Path(fixture["oni"]))
    failed = json.loads(
        core(
            fixture,
            "finalize",
            "--test-fail-after-step",
            "2",
            apply=True,
            expected={2},
        ).stdout
    )
    assert_true(failed.get("status") == "blocked", "clean rollback fixture did not fault")
    assert_true(file_inventory(pathlib.Path(fixture["oni"])) == before, "clean rollback changed ONI state")
    roots = workflow.resolve_roots(
        workflow.build_parser().parse_args(
            ["finalize", *common_args(fixture), *finalize_binding_args(fixture), "--what-if"]
        )
    )
    final_root = fixture_paths(fixture)["environment"] / "finalization"
    plan_path = final_root / "transaction-plan.json"
    binding_path = fixture_paths(fixture)["environment"] / "runtime-binding.json"
    manifest_path = fixture_paths(fixture)["environment"] / "environment-manifest.json"
    plan = workflow._validate_finalize_transaction_plan(
        json.loads(plan_path.read_text(encoding="utf-8")),
        roots=roots,
        manifest_hash=sha256(manifest_path),
        runtime_binding_sha256=sha256(binding_path),
    )
    journal = fixture_paths(fixture)["environment"] / "restore-journal.jsonl"
    original_raw = journal.read_bytes()
    original_entries = workflow.verify_journal(journal)
    assert_true(
        workflow._classify_finalize_transaction_journal(roots, plan, sha256(plan_path))
        == "clean_rollback",
        "clean rollback journal did not close its attempt epoch",
    )

    def expect_reducer_refusal(name: str, entries: list[dict[str, Any]]) -> None:
        write_resealed_journal(journal, entries)
        try:
            workflow._classify_finalize_transaction_journal(roots, plan, sha256(plan_path))
        except workflow.WorkflowError:
            pass
        else:
            raise VerificationFailure(f"finalize reducer accepted {name}")
        assert_true(file_inventory(pathlib.Path(fixture["oni"])) == before, f"{name} changed ONI state")

    start_index = next(
        index
        for index, entry in enumerate(original_entries)
        if entry.get("event") == "restore_transaction_started"
    )
    clean_attempt = original_entries[start_index:]
    rollback_step_indices = [
        index for index, entry in enumerate(clean_attempt) if entry.get("event") == "restore_rollback_step"
    ]
    assert_true(len(rollback_step_indices) == 2, "rollback grammar fixture did not restore two steps")
    absolute_rollback_indices = [start_index + index for index in rollback_step_indices]

    nonreverse = copy.deepcopy(original_entries)
    nonreverse[absolute_rollback_indices[0]]["data"]["label"], nonreverse[absolute_rollback_indices[1]]["data"]["label"] = (
        nonreverse[absolute_rollback_indices[1]]["data"]["label"],
        nonreverse[absolute_rollback_indices[0]]["data"]["label"],
    )
    expect_reducer_refusal("nonreverse rollback labels", nonreverse)

    arbitrary = copy.deepcopy(original_entries)
    arbitrary[absolute_rollback_indices[0]]["data"]["label"] = plan["steps"][-1]["label"]
    expect_reducer_refusal("arbitrary rollback label", arbitrary)

    truncated = copy.deepcopy(original_entries[:-1])
    expect_reducer_refusal("missing rollback terminal", truncated)
    write_resealed_journal(journal, truncated)
    journal_before_retry = journal.read_bytes()
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(
        blocked.get("status") == "blocked"
        and journal.read_bytes() == journal_before_retry
        and file_inventory(pathlib.Path(fixture["oni"])) == before,
        "truncated rollback journal allowed a physical retry",
    )

    duplicate_terminal = copy.deepcopy(original_entries)
    duplicate_terminal.append(copy.deepcopy(original_entries[-1]))
    expect_reducer_refusal("duplicate rollback terminal", duplicate_terminal)

    after_terminal = copy.deepcopy(original_entries)
    trailing = copy.deepcopy(
        next(entry for entry in clean_attempt if entry.get("event") == "restore_step_intent")
    )
    after_terminal.append(trailing)
    expect_reducer_refusal("event after rollback terminal", after_terminal)

    start_template = copy.deepcopy(clean_attempt[0])
    binding_fields = {
        key: start_template["data"][key]
        for key in (
            "finalize_transaction_id",
            "finalize_plan_hash",
            "finalize_plan_file_sha256",
        )
    }
    synthetic_attempt_id = "attempt-" + "f" * 32

    def synthetic_entry(event: str, data: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": workflow.JOURNAL_SCHEMA,
            "recorded_at": start_template["recorded_at"],
            "event": event,
            "data": {
                **dict(data),
                **binding_fields,
                "finalize_attempt_id": synthetic_attempt_id,
            },
            "previous_entry_hash": None,
            "index": 0,
        }

    committed_attempt = [
        synthetic_entry(
            "restore_transaction_started",
            {
                "transaction_kind": "finalize_restore",
                "expected_step_count": len(plan["steps"]),
            },
        )
    ]
    for index, step in enumerate(plan["steps"], start=1):
        committed_attempt.append(
            synthetic_entry(
                "restore_step_intent",
                {"kind": step["kind"], "label": step["label"]},
            )
        )
        committed_attempt.append(
            synthetic_entry(
                "restore_step_completed",
                {"step": index, "label": step["label"]},
            )
        )
    committed_attempt.extend(
        (
            synthetic_entry(
                "restore_transaction_commit_prepared",
                {"step_count": len(plan["steps"]), "state": "physical_committed_pending_journal"},
            ),
            synthetic_entry(
                "restore_transaction_physical_committed",
                {"step_count": len(plan["steps"]), "state": "journal_confirmed"},
            ),
        )
    )
    prefix = copy.deepcopy(original_entries[:start_index])
    coherent_forged_commit = [*prefix, *copy.deepcopy(committed_attempt)]
    write_resealed_journal(journal, coherent_forged_commit)
    assert_true(
        workflow._classify_finalize_transaction_journal(roots, plan, sha256(plan_path))
        == "physical_committed",
        "strict reducer rejected its own exact committed grammar fixture",
    )
    blocked = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(
        blocked.get("status") == "blocked" and file_inventory(pathlib.Path(fixture["oni"])) == before,
        "self-rehashed committed journal bypassed independent physical custody",
    )

    committed_mutations: dict[str, list[dict[str, Any]]] = {}
    completion_before_intent = copy.deepcopy(coherent_forged_commit)
    completion_before_intent[start_index + 1], completion_before_intent[start_index + 2] = (
        completion_before_intent[start_index + 2],
        completion_before_intent[start_index + 1],
    )
    committed_mutations["completion_before_intent"] = completion_before_intent

    out_of_order_completion = copy.deepcopy(coherent_forged_commit)
    first_completion = start_index + 2
    second_completion = start_index + 4
    out_of_order_completion[first_completion], out_of_order_completion[second_completion] = (
        out_of_order_completion[second_completion],
        out_of_order_completion[first_completion],
    )
    committed_mutations["completion_out_of_order"] = out_of_order_completion

    completion_after_commit = copy.deepcopy(coherent_forged_commit)
    moved_completion = completion_after_commit.pop(-3)
    completion_after_commit.append(moved_completion)
    committed_mutations["completion_after_commit"] = completion_after_commit

    duplicate_commit = copy.deepcopy(coherent_forged_commit)
    duplicate_commit.append(copy.deepcopy(duplicate_commit[-1]))
    committed_mutations["duplicate_physical_commit"] = duplicate_commit

    committed_event_after_terminal = copy.deepcopy(coherent_forged_commit)
    committed_event_after_terminal.append(copy.deepcopy(committed_event_after_terminal[start_index + 1]))
    committed_mutations["event_after_commit_terminal"] = committed_event_after_terminal

    bad_prepared_state = copy.deepcopy(coherent_forged_commit)
    bad_prepared_state[-2]["data"]["state"] = "forged_prepared_state"
    committed_mutations["prepared_state_tamper"] = bad_prepared_state

    bad_committed_state = copy.deepcopy(coherent_forged_commit)
    bad_committed_state[-1]["data"]["state"] = "forged_committed_state"
    committed_mutations["committed_state_tamper"] = bad_committed_state

    extra_event_field = copy.deepcopy(coherent_forged_commit)
    extra_event_field[start_index + 1]["data"]["extra"] = True
    committed_mutations["event_data_extra_key"] = extra_event_field

    invalid_attempt_id = copy.deepcopy(coherent_forged_commit)
    for entry in invalid_attempt_id[start_index:]:
        entry["data"]["finalize_attempt_id"] = "attempt-not-hex"
    committed_mutations["attempt_id_pattern"] = invalid_attempt_id

    top_level_extra = copy.deepcopy(coherent_forged_commit)
    top_level_extra[start_index + 1]["extra"] = True
    committed_mutations["journal_entry_extra_key"] = top_level_extra

    interleaved_foreign = copy.deepcopy(coherent_forged_commit)
    foreign_entry = {
        "schema": workflow.JOURNAL_SCHEMA,
        "recorded_at": start_template["recorded_at"],
        "event": "foreign_interleaved_audit_event",
        "data": {"value": 1},
        "previous_entry_hash": None,
        "index": 0,
    }
    interleaved_foreign.insert(start_index + 2, foreign_entry)
    committed_mutations["foreign_event_interleaved"] = interleaved_foreign

    interleaved_abort = copy.deepcopy(coherent_forged_commit)
    abort_entry = copy.deepcopy(foreign_entry)
    abort_entry["event"] = "restore_step_intent"
    abort_entry["data"] = {
        "kind": "move",
        "label": "abort_foreign",
        "abort_transaction_id": "abort-foreign",
        "abort_plan_hash": "sha256:" + "0" * 64,
    }
    interleaved_abort.insert(start_index + 2, abort_entry)
    committed_mutations["abort_restore_event_interleaved"] = interleaved_abort
    for name, entries in committed_mutations.items():
        expect_reducer_refusal(name, entries)

    forged_plan_id = copy.deepcopy(plan)
    forged_plan_id["transaction_id"] = "finalize-" + "0" * 32
    forged_plan_id["plan_hash"] = workflow.contract_hash_without(
        forged_plan_id,
        "plan_hash",
        "forged transaction plan",
    )
    try:
        workflow._validate_finalize_transaction_plan(
            forged_plan_id,
            roots=roots,
            manifest_hash=sha256(manifest_path),
            runtime_binding_sha256=sha256(binding_path),
        )
        raise VerificationFailure("self-rehashed transaction id tamper was accepted")
    except workflow.WorkflowError:
        pass

    fake_cross_epoch = copy.deepcopy(original_entries)
    fake_cross_epoch.extend(
        (
            synthetic_entry(
                "restore_transaction_started",
                {
                    "transaction_kind": "finalize_restore",
                    "expected_step_count": len(plan["steps"]),
                },
            ),
            synthetic_entry(
                "restore_transaction_commit_prepared",
                {"step_count": len(plan["steps"]), "state": "physical_committed_pending_journal"},
            ),
            synthetic_entry(
                "restore_transaction_physical_committed",
                {"step_count": len(plan["steps"]), "state": "journal_confirmed"},
            ),
        )
    )
    expect_reducer_refusal("cross-epoch commit without steps", fake_cross_epoch)

    write_bytes(journal, original_raw)
    recovered = json.loads(core(fixture, "finalize", apply=True).stdout)
    assert_true(recovered.get("status") == "finalized", "valid clean rollback could not open a new attempt")

    latch_fixture = make_fixture(base, "finalize-rollback-latch", run_id="g0-finalize-rollback-latch")
    prepare_fixture(latch_fixture)
    simulate_runtime(latch_fixture)
    latch_before = file_inventory(pathlib.Path(latch_fixture["oni"]))
    latch_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(latch_fixture),
            *finalize_binding_args(latch_fixture),
            "--test-fail-after-step",
            "1",
            "--apply",
        ]
    )
    latch_roots = workflow.resolve_roots(latch_args)
    original_append = workflow.append_journal
    fired = False

    def fault_rollback_step(
        path: pathlib.Path,
        event: str,
        data: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal fired

        def fault(stage: str) -> None:
            nonlocal fired
            if not fired and event == "restore_rollback_step" and stage == "after_fsync":
                fired = True
                raise RuntimeError("injected finalize rollback-step journal fault")

        return original_append(path, event, data, fault_hook=fault, **kwargs)

    workflow.append_journal = fault_rollback_step
    try:
        try:
            with workflow.exclusive_finalize_lock(latch_roots):
                workflow.finalize_locked(latch_args, latch_roots)
            raise VerificationFailure("rollback-step journal fault did not block finalize")
        except workflow.WorkflowError:
            pass
    finally:
        workflow.append_journal = original_append
    latch_path = fixture_paths(latch_fixture)["environment"] / "finalization" / "transaction-recovery.json"
    assert_true(
        fired and latch_path.is_file() and file_inventory(pathlib.Path(latch_fixture["oni"])) == latch_before,
        "rollback journal fault did not restore ONI state and publish a latch",
    )
    latch_journal = fixture_paths(latch_fixture)["environment"] / "restore-journal.jsonl"
    latch_journal_before = latch_journal.read_bytes()
    blocked_retry = json.loads(core(latch_fixture, "finalize", apply=True, expected={2}).stdout)
    assert_true(
        blocked_retry.get("status") == "blocked"
        and latch_journal.read_bytes() == latch_journal_before
        and file_inventory(pathlib.Path(latch_fixture["oni"])) == latch_before,
        "transaction recovery latch allowed a second physical attempt",
    )
    return {
        "strict_committed_grammar_attacks_refused": sorted(committed_mutations),
        "strict_rollback_reverse_prefix_attacks_refused": 5,
        "cross_epoch_commit_without_steps": "refused",
        "coherent_rehashed_commit_without_physical_state": "custody_refused",
        "clean_rollback_new_attempt": "passed",
        "rollback_journal_fault_latch": "retry_blocked_zero_physical_replay",
    }


def test_atomic_publication_and_prepare_stage_ownership(base: pathlib.Path) -> dict[str, Any]:
    atomic_case = base / "atomic-json-ownership"
    atomic_case.mkdir()
    target = atomic_case / "evidence.json"
    recreated_temps: list[pathlib.Path] = []

    def recreate_temp(_path: pathlib.Path, temporary: pathlib.Path) -> None:
        write_bytes(temporary, b"external-post-publication-temp\n")
        recreated_temps.append(temporary)

    workflow.write_json_atomic(
        target,
        {"schema": "fixture.atomic.v1", "value": 1},
        create_only=True,
        after_publish_hook=recreate_temp,
    )
    assert_true(
        recreated_temps and recreated_temps[0].read_bytes() == b"external-post-publication-temp\n",
        "post-publication JSON temp replacement was deleted",
    )

    swap_target = atomic_case / "swap-evidence.json"
    replacement = atomic_case / "external-replacement.json"
    moved_owner = atomic_case / "owned-publication-moved.json"
    replacement_payload = b'{"external":true}\n'
    write_bytes(replacement, replacement_payload)

    def swap_publication(path: pathlib.Path, _temporary: pathlib.Path) -> None:
        os.replace(path, moved_owner)
        os.replace(replacement, path)

    try:
        workflow.write_json_atomic(
            swap_target,
            {"schema": "fixture.atomic.v1", "value": 2},
            create_only=True,
            after_publish_hook=swap_publication,
        )
        raise VerificationFailure("atomic JSON target replacement was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(swap_target.read_bytes() == replacement_payload, "atomic JSON replacement was deleted")

    link_fault_target = atomic_case / "link-unlink-fault.json"
    link_external = atomic_case / "link-unlink-external.json"
    link_owned_alias = atomic_case / "link-unlink-owned-alias.json"
    link_external_payload = b'{"external_link_fault":true}\n'
    write_bytes(link_external, link_external_payload)

    def fail_between_link_unlink(path: pathlib.Path, _temporary: pathlib.Path) -> None:
        os.replace(path, link_owned_alias)
        os.replace(link_external, path)
        raise OSError("injected link-to-unlink failure")

    try:
        workflow.write_json_atomic(
            link_fault_target,
            {"schema": "fixture.atomic.v1", "value": 22},
            create_only=True,
            before_link_unlink_hook=fail_between_link_unlink,
        )
        raise VerificationFailure("create-only link/unlink fault was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(
        link_fault_target.read_bytes() == link_external_payload and link_owned_alias.exists(),
        "link/unlink fault deleted the external replacement or lost owned recovery inode",
    )

    temp_swap_target = atomic_case / "link-temp-swap.json"
    temp_swap_external = atomic_case / "link-temp-external.json"
    temp_swap_owned = atomic_case / "link-temp-owned.json"
    temp_swap_payload = b"external-link-temp-must-survive\n"
    write_bytes(temp_swap_external, temp_swap_payload)
    observed_temp: list[pathlib.Path] = []

    def swap_link_temporary(_path: pathlib.Path, temporary: pathlib.Path) -> None:
        os.replace(temporary, temp_swap_owned)
        os.replace(temp_swap_external, temporary)
        observed_temp.append(temporary)

    try:
        workflow.write_json_atomic(
            temp_swap_target,
            {"schema": "fixture.atomic.v1", "value": 23},
            create_only=True,
            before_link_unlink_hook=swap_link_temporary,
        )
        raise VerificationFailure("create-only temporary name swap was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(
        observed_temp
        and observed_temp[0].read_bytes() == temp_swap_payload
        and temp_swap_owned.exists(),
        "link temporary replacement was deleted or owned alias lost",
    )

    post_verify_target = atomic_case / "post-verify.json"

    def fail_post_verify(_path: pathlib.Path) -> None:
        raise RuntimeError("deterministic post-verify publication failure")

    try:
        workflow.write_json_atomic(
            post_verify_target,
            {"schema": "fixture.atomic.v1", "value": 3},
            create_only=True,
            after_verify_hook=fail_post_verify,
        )
        raise VerificationFailure("post-verify JSON fault was accepted")
    except RuntimeError:
        pass
    assert_true(not post_verify_target.exists(), "owned post-verify publication was not safely removed")

    rename_once_case = atomic_case / "rename-once"
    rename_once_case.mkdir()
    rename_once_target = rename_once_case / "result.json"
    rename_once_value = {"schema": "fixture.rename-once.v1", "value": 1}
    workflow.write_json_once_by_rename(
        rename_once_target,
        rename_once_value,
        "rename-once fixture",
    )
    rename_once_bytes = rename_once_target.read_bytes()
    rename_once_identity = workflow._owned_node_identity(os.lstat(rename_once_target))
    assert_true(
        os.lstat(rename_once_target).st_nlink == 1
        and not list(rename_once_case.glob(".j-*.tmp")),
        "rename-once publication left a hard-link or staging crash window",
    )
    workflow.write_json_once_by_rename(
        rename_once_target,
        rename_once_value,
        "rename-once fixture",
    )
    assert_true(
        rename_once_target.read_bytes() == rename_once_bytes
        and workflow._owned_node_identity(os.lstat(rename_once_target))
        == rename_once_identity,
        "rename-once exact retry did not converge to the existing publication",
    )
    try:
        workflow.write_json_once_by_rename(
            rename_once_target,
            {"schema": "fixture.rename-once.v1", "value": 2},
            "rename-once fixture",
        )
        raise VerificationFailure("rename-once conflicting retry was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(
        rename_once_target.read_bytes() == rename_once_bytes,
        "rename-once conflict changed the immutable publication",
    )
    rename_once_alias = rename_once_case / "result-alias.json"
    os.link(rename_once_target, rename_once_alias)
    try:
        workflow.write_json_once_by_rename(
            rename_once_target,
            rename_once_value,
            "rename-once hardlink fixture",
        )
        raise VerificationFailure("rename-once hardlinked existing target was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(
        rename_once_target.read_bytes() == rename_once_bytes
        and rename_once_alias.read_bytes() == rename_once_bytes
        and os.lstat(rename_once_target).st_nlink == 2,
        "rename-once hardlink refusal changed either alias",
    )
    rename_once_alias.unlink()

    rename_fault_target = rename_once_case / "fault.json"

    def fail_rename_once_post_verify(_path: pathlib.Path) -> None:
        raise OSError("injected rename-once post-verification failure")

    try:
        workflow.write_json_once_by_rename(
            rename_fault_target,
            rename_once_value,
            "rename-once fault fixture",
            after_verify_hook=fail_rename_once_post_verify,
        )
        raise VerificationFailure("rename-once post-verification fault was accepted")
    except OSError:
        pass
    assert_true(
        not rename_fault_target.exists()
        and not list(rename_once_case.glob(".j-*.tmp")),
        "rename-once fault retained an executor-visible target or staging file",
    )
    workflow.write_json_once_by_rename(
        rename_fault_target,
        rename_once_value,
        "rename-once fault fixture",
    )
    assert_true(
        rename_fault_target.read_bytes() == rename_once_bytes
        and os.lstat(rename_fault_target).st_nlink == 1,
        "rename-once fault retry did not converge safely",
    )

    preexisting_target = atomic_case / "preexisting-evidence.json"
    preexisting_payload = b'{"external_preexisting":true}\n'
    write_bytes(preexisting_target, preexisting_payload)
    try:
        workflow.write_json_atomic(preexisting_target, {"value": "must-not-overwrite"})
        raise VerificationFailure("preexisting JSON publication was overwritten")
    except workflow.WorkflowError:
        pass
    assert_true(preexisting_target.read_bytes() == preexisting_payload, "preexisting JSON changed")

    late_target = atomic_case / "late-target.json"
    late_payload = b'{"external_late":true}\n'

    def create_late_target(path: pathlib.Path, _temporary: pathlib.Path) -> None:
        write_bytes(path, late_payload)

    try:
        workflow.write_json_atomic(
            late_target,
            {"value": "must-not-overwrite"},
            before_publish_hook=create_late_target,
        )
        raise VerificationFailure("late JSON target was overwritten")
    except workflow.WorkflowError:
        pass
    assert_true(late_target.read_bytes() == late_payload, "late JSON target changed")

    cleanup_file = atomic_case / "cleanup-file.tmp"
    cleanup_file_moved = atomic_case / "cleanup-file-owned.tmp"
    cleanup_file_external = atomic_case / "cleanup-file-external.tmp"
    cleanup_file_payload = b"cleanup-file-external\n"
    write_bytes(cleanup_file, b"owned-cleanup-file\n")
    cleanup_identity = workflow._owned_node_identity(os.lstat(cleanup_file))
    write_bytes(cleanup_file_external, cleanup_file_payload)

    def swap_cleanup_file(path: pathlib.Path) -> None:
        os.replace(path, cleanup_file_moved)
        os.replace(cleanup_file_external, path)

    try:
        workflow._unlink_owned_file(
            cleanup_file, cleanup_identity, before_claim_hook=swap_cleanup_file
        )
        raise VerificationFailure("cleanup file replacement was deleted")
    except workflow.WorkflowError:
        pass
    assert_true(cleanup_file.read_bytes() == cleanup_file_payload, "cleanup file replacement changed")

    cleanup_tree = atomic_case / "cleanup-tree.tmp"
    write_bytes(cleanup_tree / "owned.txt", b"owned-tree\n")
    cleanup_tree_owner = workflow.capture_transaction_node(cleanup_tree)

    def inject_cleanup_child(path: pathlib.Path) -> None:
        write_bytes(path / "external-injected.txt", b"external-injected-child\n")

    try:
        workflow._remove_owned_tree(
            cleanup_tree, cleanup_tree_owner, before_claim_hook=inject_cleanup_child
        )
        raise VerificationFailure("cleanup tree with injected child was deleted")
    except workflow.WorkflowError:
        pass
    assert_true(
        (cleanup_tree / "external-injected.txt").read_bytes() == b"external-injected-child\n",
        "cleanup tree injected child was deleted",
    )

    evidence_source = atomic_case / "source-evidence.json"
    frozen_target = atomic_case / "frozen-evidence.json"
    frozen_alias = atomic_case / "frozen-evidence-alias.json"
    write_bytes(evidence_source, b"immutable-evidence\n")
    write_bytes(frozen_target, b"immutable-evidence\n")
    os.link(frozen_target, frozen_alias)
    try:
        workflow.copy_evidence_once(evidence_source, frozen_target, sha256(evidence_source))
        raise VerificationFailure("hardlinked existing frozen evidence was accepted")
    except workflow.WorkflowError:
        hardlink = "refused"

    prepare_fixture_data = make_fixture(base, "prepare-stage-replacement", run_id="g0-prepare-stage-replacement")
    prepare_args = workflow.build_parser().parse_args(
        ["prepare", *common_args(prepare_fixture_data), "--apply"]
    )
    original_copy = workflow.copy_file_stable
    replacement_stage: pathlib.Path | None = None
    moved_stage: pathlib.Path | None = None

    def replace_prepare_stage(
        _source: pathlib.Path,
        destination: pathlib.Path,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal replacement_stage, moved_stage
        stage = destination
        while not stage.name.startswith(".prep-"):
            stage = stage.parent
        replacement_stage = stage
        moved_stage = stage.parent / ".owned-prepare-stage-moved"
        external = stage.parent / ".external-prepare-stage-replacement"
        write_bytes(external / "external.txt", b"external-stage-must-survive\n")
        os.replace(stage, moved_stage)
        os.replace(external, stage)
        raise workflow.WorkflowError("injected prepare stage replacement")

    workflow.copy_file_stable = replace_prepare_stage
    try:
        try:
            workflow.prepare(prepare_args)
            raise VerificationFailure("prepare stage replacement was accepted")
        except workflow.WorkflowError:
            pass
    finally:
        workflow.copy_file_stable = original_copy
    assert_true(
        replacement_stage is not None
        and (replacement_stage / "external.txt").read_bytes() == b"external-stage-must-survive\n"
        and moved_stage is not None
        and moved_stage.exists(),
        "prepare cleanup deleted an external stage replacement",
    )
    return {
        "atomic_temp_recreation": "retained",
        "atomic_target_replacement": "retained_and_outcome_unknown",
        "atomic_link_unlink_fault": "replacement_retained_and_outcome_unknown",
        "atomic_link_temporary_name_swap": "replacement_retained_and_outcome_unknown",
        "atomic_post_verify_failure": "owned_target_removed",
        "rename_once_publication": "single_link_exact_retry_converged",
        "rename_once_conflict": "refused_unmodified",
        "rename_once_existing_hardlink": "refused_unmodified",
        "rename_once_post_verify_fault": "revoked_then_retry_converged",
        "preexisting_json_target": "conflict_refused_unmodified",
        "late_json_target": "create_only_refused_unmodified",
        "cleanup_file_after_check_swap": "atomic_claim_refused_replacement_retained",
        "cleanup_tree_unexpected_child": "recovery_tree_retained",
        "prepare_stage_replacement": "retained",
        "existing_frozen_evidence_hardlink": hardlink,
    }


def test_copy_staging_descriptor_ownership(base: pathlib.Path) -> dict[str, Any]:
    case = base / "copy-staging-descriptor-ownership"
    case.mkdir()
    source = case / "source.bin"
    target = case / "target.bin"
    write_bytes(source, b"A" * (2 * 1024 * 1024 + 17))

    occupied: list[pathlib.Path] = []

    def occupy(temporary: pathlib.Path) -> None:
        write_bytes(temporary, b"foreign-staging-owner")
        occupied.append(temporary)

    try:
        workflow.copy_file_stable(source, target, before_temporary_open_hook=occupy)
        raise VerificationFailure("copy staging CREATE_NEW race was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(
        not target.exists()
        and occupied
        and occupied[0].read_bytes() == b"foreign-staging-owner",
        "copy staging race overwrote or deleted the foreign file",
    )

    partial_target = case / "partial-target.bin"

    def fail_after_chunk(_temporary: pathlib.Path, copied: int) -> None:
        if copied:
            raise OSError("injected descriptor-copy partial failure")

    try:
        workflow.copy_file_stable(source, partial_target, after_copy_chunk_hook=fail_after_chunk)
        raise VerificationFailure("partial descriptor copy unexpectedly succeeded")
    except OSError:
        pass
    assert_true(
        not partial_target.exists()
        and set(case.glob(".c-*.tmp")) == set(occupied),
        "partial descriptor copy left an unowned staging file",
    )

    disappeared_target = case / "source-open-target.bin"
    displaced_source = case / "source-displaced.bin"

    def remove_source_before_open(_temporary: pathlib.Path) -> None:
        os.replace(source, displaced_source)

    try:
        workflow.copy_file_stable(
            source,
            disappeared_target,
            before_temporary_open_hook=remove_source_before_open,
        )
        raise VerificationFailure("source-open fault unexpectedly succeeded")
    except workflow.WorkflowError:
        pass
    finally:
        os.replace(displaced_source, source)
    assert_true(
        not disappeared_target.exists()
        and set(case.glob(".c-*.tmp")) == set(occupied),
        "source-open failure left the create-only staging file behind",
    )

    tree_source = case / "tree-source"
    write_bytes(tree_source / "a.bin", b"a")
    write_bytes(tree_source / "nested" / "b.bin", b"b")
    tree_snapshot = workflow.snapshot_tree(tree_source)
    tree_target = case / "tree-target"

    def fail_tree_file(_path: pathlib.Path, index: int) -> None:
        if index == 1:
            raise OSError("injected tree staging failure")

    try:
        workflow.copy_tree_exact(
            tree_source,
            tree_target,
            tree_snapshot,
            after_file_hook=fail_tree_file,
        )
        raise VerificationFailure("partial tree copy unexpectedly succeeded")
    except OSError:
        pass
    assert_true(
        not tree_target.exists()
        and not list(case.glob(".t-*.tmp")),
        "partial tree staging was not ownership-cleaned",
    )

    injected_target = case / "tree-injected"
    injected_payload = b"external-tree-child"

    def inject_tree_child(path: pathlib.Path, index: int) -> None:
        if index == 1:
            write_bytes(path.parent / "external-child.bin", injected_payload)
            raise OSError("injected external tree child")

    try:
        workflow.copy_tree_exact(
            tree_source,
            injected_target,
            tree_snapshot,
            after_file_hook=inject_tree_child,
        )
        raise VerificationFailure("tree staging external child race unexpectedly succeeded")
    except workflow.WorkflowError as exc:
        assert_true("outcome_unknown" in str(exc), "tree external-child failure was not latched")
    retained = [path for path in case.rglob("external-child.bin") if path.read_bytes() == injected_payload]
    assert_true(retained, "tree staging cleanup deleted the external child")
    return {
        "file_create_new_race": "foreign_file_retained",
        "partial_descriptor_copy": "owned_staging_removed",
        "source_open_failure": "owned_staging_removed",
        "partial_tree_copy": "owned_nodes_removed",
        "tree_external_child": "retained_with_outcome_unknown",
    }


def test_finalize_result_nonreplay_recovery(base: pathlib.Path) -> dict[str, Any]:
    fixture = make_fixture(base, "finalize-result-recovery", run_id="g0-finalize-result-recovery")
    prepare_fixture(fixture)
    runtime = simulate_runtime(fixture)
    blocked = json.loads(
        core(
            fixture,
            "finalize",
            "--test-finalize-result-fail-after-verify",
            apply=True,
            expected={2},
        ).stdout
    )
    assert_true(blocked.get("status") == "blocked", "finalize-result publication fault was accepted")
    environment = fixture_paths(fixture)["environment"]
    final_root = environment / "finalization"
    recovery_path = final_root / "finalize-recovery.json"
    result_path = final_root / "finalize-result.json"
    assert_true(recovery_path.exists() and not result_path.exists(), "non-replay recovery state was not published")
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert_true(
        recovery.get("state") == "finalize_result_publication_outcome_unknown"
        and isinstance(recovery.get("candidate_result"), dict)
        and recovery.get("physical_commit_non_replay") is True
        and recovery.get("runtime_config")
        == recovery["candidate_result"].get("runtime_config")
        and recovery.get("runtime_config_binding_hash")
        == recovery["candidate_result"].get("runtime_config_binding_hash")
        and recovery.get("runtime_config_evidence_sha256")
        == fixture["runtime_config_evidence_sha256"]
        and recovery.get("expected_release_dll_sha256")
        == fixture["expected_release_dll_sha256"]
        and recovery.get("normal_shutdown_evidence_hash")
        == fixture["shutdown_evidence_hash"]
        and recovery.get("startup_consumption_evidence_hash")
        == fixture["startup_consumption_evidence_hash"]
        and recovery.get("validation_process_id") == fixture["validation_process_id"]
        and recovery.get("startup_session_id")
        == recovery["candidate_result"].get("startup_session_id")
        and recovery.get("validation_process_creation_token")
        == recovery["candidate_result"].get("validation_process_creation_token")
        and recovery.get("startup_archive_prefix_sha256")
        == recovery["candidate_result"].get("startup_archive_prefix_sha256")
        and recovery.get("startup_archive_prefix_length")
        == recovery["candidate_result"].get("startup_archive_prefix_length"),
        "finalize-result recovery did not freeze the shutdown/startup-bound candidate result",
    )
    assert_true(
        set(recovery) == workflow.FINALIZE_RECOVERY_KEYS
        and recovery.get("document_hash")
        == workflow.contract_hash_without(recovery, "document_hash", "fixture finalize recovery")
        and recovery.get("candidate_result_hash")
        == workflow.contract_canonical_hash(recovery["candidate_result"])
        and set(recovery["candidate_result"]) == workflow.FINALIZE_RESULT_KEYS
        and recovery["candidate_result"].get("document_hash")
        == workflow.contract_hash_without(
            recovery["candidate_result"],
            "document_hash",
            "fixture finalize candidate",
        ),
        "finalize recovery/candidate exact shapes or self-hashes were not sealed",
    )
    frozen_config = final_root / "pair" / "config.json"
    assert_true(
        sha256(frozen_config) == fixture["runtime_config_evidence_sha256"],
        "non-replay recovery lost the frozen runtime config evidence",
    )
    assert_true(
        not runtime["derivative"].exists()
        and (environment / "rb").is_dir()
        and not pathlib.Path(fixture["startup_consumption_bundle"]).exists()
        and (
            environment
            / "rb"
            / pathlib.Path(
                json.loads((environment / "runtime-binding.json").read_text(encoding="utf-8"))[
                    "startup_consumption"
                ]["relative"]
            )
        ).is_dir()
        and (pathlib.Path(fixture["oni"]) / "ai-bridge" / "old-runtime-marker.txt").exists(),
        "physical commit did not complete before finalize-result fault",
    )
    anchor_refusals: dict[str, str] = {}
    for key, wrong_value in (
        (
            "startup_consumption_bundle",
            pathlib.Path(fixture["startup_consumption_bundle"]).with_name("wrong-startup-bundle"),
        ),
        ("startup_consumption_evidence_hash", "sha256:" + "e" * 64),
        ("validation_process_id", int(fixture["validation_process_id"]) + 1),
        ("shutdown_evidence_hash", "sha256:" + "d" * 64),
    ):
        original = fixture[key]
        fixture[key] = wrong_value
        try:
            mismatch = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
            assert_true(mismatch.get("status") == "blocked", f"wrong recovery {key} anchor was accepted")
        finally:
            fixture[key] = original
        anchor_refusals[key] = "refused_without_replay"
    recovery_payload = recovery_path.read_bytes()

    def assert_resealed_recovery_refused(
        label: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        refresh_candidate_hash: bool = True,
    ) -> None:
        attack_payload = json.loads(recovery_payload.decode("utf-8"))
        mutate(attack_payload)
        candidate_payload = attack_payload.get("candidate_result")
        if refresh_candidate_hash and isinstance(candidate_payload, dict):
            attack_payload["candidate_result_hash"] = workflow.contract_canonical_hash(
                candidate_payload
            )
        attack_payload["document_hash"] = workflow.contract_hash_without(
            attack_payload,
            "document_hash",
            f"{label} recovery",
        )
        write_json(recovery_path, attack_payload)
        try:
            refused = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
            assert_true(refused.get("status") == "blocked", f"{label} was accepted")
        finally:
            write_bytes(recovery_path, recovery_payload)

    assert_resealed_recovery_refused(
        "self-rehashed recovery extra key",
        lambda payload: payload.update({"unexpected": "field"}),
    )

    def add_candidate_extra(payload: dict[str, Any]) -> None:
        candidate_payload = payload["candidate_result"]
        candidate_payload["unexpected"] = "field"
        candidate_payload["document_hash"] = workflow.contract_hash_without(
            candidate_payload,
            "document_hash",
            "candidate extra-key attack",
        )

    assert_resealed_recovery_refused(
        "self-rehashed candidate extra key",
        add_candidate_extra,
    )

    def change_post_boundary_status(payload: dict[str, Any]) -> None:
        candidate_payload = payload["candidate_result"]
        candidate_payload["post_boundary_status"] = "skipped"
        candidate_payload["document_hash"] = workflow.contract_hash_without(
            candidate_payload,
            "document_hash",
            "candidate post-boundary attack",
        )

    assert_resealed_recovery_refused(
        "self-rehashed candidate post_boundary_status",
        change_post_boundary_status,
    )

    def change_candidate_config_count(payload: dict[str, Any]) -> None:
        candidate_payload = payload["candidate_result"]
        candidate_payload["config_count"] = 999
        candidate_payload["document_hash"] = workflow.contract_hash_without(
            candidate_payload,
            "document_hash",
            "candidate config-count attack",
        )

    assert_resealed_recovery_refused(
        "self-rehashed candidate config_count",
        change_candidate_config_count,
    )

    def flip_candidate_old_bridge(payload: dict[str, Any]) -> None:
        candidate_payload = payload["candidate_result"]
        candidate_payload["old_bridge_restored"] = not bool(
            candidate_payload["old_bridge_restored"]
        )
        candidate_payload["document_hash"] = workflow.contract_hash_without(
            candidate_payload,
            "document_hash",
            "candidate old-bridge attack",
        )

    assert_resealed_recovery_refused(
        "self-rehashed candidate old_bridge_restored",
        flip_candidate_old_bridge,
    )
    assert_resealed_recovery_refused(
        "self-rehashed no-candidate state with injected candidate",
        lambda payload: payload.update(
            {"state": "post_boundary_or_publication_blocked_after_physical_commit"}
        ),
    )
    assert_resealed_recovery_refused(
        "candidate hash mismatch",
        lambda payload: payload.update({"candidate_result_hash": "sha256:" + "0" * 64}),
        refresh_candidate_hash=False,
    )

    tampered_recovery = json.loads(recovery_payload.decode("utf-8"))
    del tampered_recovery["runtime_config"]["bootstrap"]
    tampered_recovery["document_hash"] = workflow.contract_hash_without(
        tampered_recovery,
        "document_hash",
        "runtime config binding mismatch recovery",
    )
    write_json(recovery_path, tampered_recovery)
    mismatch = json.loads(core(fixture, "finalize", what_if=True, expected={2}).stdout)
    assert_true(mismatch.get("status") == "blocked", "recovery/config binding mismatch was accepted")
    write_bytes(recovery_path, recovery_payload)
    convergence_plan = json.loads(core(fixture, "finalize", what_if=True).stdout)
    assert_true(
        convergence_plan.get("status") == "would_fresh_revalidate_committed_result"
        and convergence_plan.get("minimum_write_count") == 4
        and convergence_plan.get("fresh_post_boundary_required") is True
        and convergence_plan.get("stored_candidate_authorizes_publication") is False
        and convergence_plan.get("physical_transaction_replayed") is False
        and convergence_plan.get("runtime_config_binding_hash")
        == recovery["runtime_config_binding_hash"],
        "non-replay WhatIf did not require fresh read-only revalidation",
    )
    recovered = json.loads(core(fixture, "finalize", apply=True).stdout)
    assert_true(recovered.get("status") == "finalized" and result_path.exists(), "retry did not converge candidate result")
    revalidation_report = final_root / "revalidation-comparison.json"
    revalidation_transition = final_root / "revalidation-passed.json"
    transition = json.loads(revalidation_transition.read_text(encoding="utf-8"))
    assert_true(
        revalidation_report.is_file()
        and set(transition) == workflow.FINALIZE_REVALIDATION_KEYS
        and transition.get("schema") == workflow.FINALIZE_REVALIDATION_SCHEMA
        and transition.get("status") == "passed"
        and transition.get("post_boundary_report_sha256") == sha256(revalidation_report)
        and transition.get("candidate_result") == recovered
        and transition.get("candidate_result_hash")
        == workflow.contract_canonical_hash(recovered)
        and transition.get("document_hash")
        == workflow.contract_hash_without(
            transition,
            "document_hash",
            "fixture finalize revalidation transition",
        ),
        "recovery did not publish an exact create-only fresh-revalidation transition",
    )
    assert_true(
        set(recovered) == workflow.FINALIZE_RESULT_KEYS
        and recovered.get("document_hash")
        == workflow.contract_hash_without(recovered, "document_hash", "recovered finalize result"),
        "recovered finalize result lost its exact shape or self-hash",
    )
    result_hash = sha256(result_path)
    third = json.loads(core(fixture, "finalize", apply=True).stdout)
    assert_true(third == recovered and sha256(result_path) == result_hash, "completed retry replayed or changed result")
    assert_true(
        workflow.contract_canonical_hash(recovered)
        == workflow.contract_canonical_hash(recovery["candidate_result"]),
        "recovered result differs from the original committed candidate",
    )
    journal_records = [
        json.loads(line)
        for line in (environment / "restore-journal.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    journal_events = [record.get("event") for record in journal_records]
    assert_true(
        journal_events.count("restore_transaction_physical_committed") == 1,
        "non-replay retries repeated the physical restore transaction",
    )
    recovered_terminal = next(
        (
            record
            for record in reversed(journal_records)
            if record.get("event") == "environment_finalized_recovered"
        ),
        None,
    )
    recovered_data = recovered_terminal.get("data") if isinstance(recovered_terminal, dict) else None
    assert_true(
        isinstance(recovered_data, dict)
        and recovered_data.get("normal_shutdown_evidence_hash")
        == recovered["normal_shutdown_evidence_hash"]
        and recovered_data.get("startup_consumption_evidence_hash")
        == recovered["startup_consumption_evidence_hash"]
        and recovered_data.get("startup_session_id") == recovered["startup_session_id"]
        and recovered_data.get("validation_process_creation_token")
        == recovered["validation_process_creation_token"]
        and recovered_data.get("startup_archive_prefix_sha256")
        == recovered["startup_archive_prefix_sha256"]
        and recovered_data.get("startup_archive_prefix_length")
        == recovered["startup_archive_prefix_length"]
        and recovered_data.get("finalize_result_sha256") == sha256(result_path),
        "recovered terminal journal did not repeat shutdown/startup custody anchors",
    )

    crash = make_fixture(base, "ir", run_id="g0-ir")
    prepare_fixture(crash)
    simulate_runtime(crash)
    crash_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(crash),
            *finalize_binding_args(crash),
            "--apply",
        ]
    )
    crash_roots = workflow.resolve_roots(crash_args)
    original_append_journal = workflow.append_journal

    def crash_before_terminal_journal(
        path: pathlib.Path,
        event: str,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        if event == "environment_finalized":
            # Model abrupt process death rather than an ordinary Exception:
            # the result rename is durable, but neither recovery nor terminal
            # journal publication has run.
            raise KeyboardInterrupt("injected crash after finalize-result publication")
        return dict(original_append_journal(path, event, data))

    workflow.append_journal = crash_before_terminal_journal
    try:
        try:
            with workflow.exclusive_finalize_lock(crash_roots):
                workflow.finalize_locked(crash_args, crash_roots)
            raise VerificationFailure("post-result crash fixture unexpectedly returned finalized")
        except KeyboardInterrupt:
            pass
    finally:
        workflow.append_journal = original_append_journal
    crash_final = crash_roots.environment_dir / "finalization"
    crash_result_path = crash_final / "finalize-result.json"
    crash_recovery_path = crash_final / "finalize-recovery.json"
    crash_result = json.loads(crash_result_path.read_text(encoding="utf-8"))
    crash_result_sha256 = sha256(crash_result_path)
    assert_true(
        crash_result.get("status") == "finalized"
        and not crash_recovery_path.exists()
        and not workflow._verify_finalize_terminal_journal(
            crash_roots,
            crash_result_path,
            crash_result,
        ),
        "post-result crash fixture did not stop in the intended no-recovery gap",
    )
    crash_retry = json.loads(core(crash, "finalize", apply=True).stdout)
    crash_third = json.loads(core(crash, "finalize", apply=True).stdout)
    crash_transition = json.loads(
        (crash_final / "revalidation-passed.json").read_text(encoding="utf-8")
    )
    crash_journal = workflow.verify_journal(
        crash_roots.environment_dir / "restore-journal.jsonl"
    )
    assert_true(
        crash_retry == crash_result
        and crash_third == crash_result
        and sha256(crash_result_path) == crash_result_sha256
        and crash_transition.get("source_recovery_sha256") is None
        and crash_transition.get("source_recovery_state")
        == workflow.IMPLICIT_POST_COMMIT_REVALIDATION_STATE
        and sum(
            entry.get("event") == "restore_transaction_physical_committed"
            for entry in crash_journal
        )
        == 1,
        "no-recovery post-result crash did not fresh-revalidate and converge idempotently",
    )

    attack = make_fixture(base, "finalize-wrapper-rehash", run_id="g0-finalize-wrapper-rehash")
    prepare_fixture(attack)
    simulate_runtime(attack)
    core(
        attack,
        "finalize",
        "--test-finalize-result-fail-after-verify",
        apply=True,
        expected={2},
    )
    attack_environment = fixture_paths(attack)["environment"]
    attack_final = attack_environment / "finalization"
    attack_binding_path = attack_environment / "runtime-binding.json"
    attack_binding = json.loads(attack_binding_path.read_text(encoding="utf-8"))
    attack_recovery_path = attack_final / "finalize-recovery.json"
    attack_recovery = json.loads(attack_recovery_path.read_text(encoding="utf-8"))
    attack_certificate = attack_binding["startup_consumption"]["certificate"]
    attack_certificate["session"]["session_id"] = "self-rehashed-wrapper-session"
    attack_binding["startup_consumption"]["session_id"] = "self-rehashed-wrapper-session"
    attack_certificate_hash = workflow.contract_canonical_hash(attack_certificate)
    attack_binding["startup_consumption"]["certificate_hash"] = attack_certificate_hash
    frozen_shutdown_path = attack_final / "pair" / "shutdown.json"
    frozen_shutdown = json.loads(frozen_shutdown_path.read_text(encoding="utf-8"))
    frozen_shutdown["startup_consumption_certificate"] = attack_certificate
    frozen_shutdown["evidence_hash"] = workflow.contract_hash_without(
        frozen_shutdown,
        "evidence_hash",
        "self-rehashed frozen shutdown",
    )
    write_json(frozen_shutdown_path, frozen_shutdown)
    attack_binding["normal_shutdown_evidence"]["file_sha256"] = sha256(frozen_shutdown_path)
    attack_binding["normal_shutdown_evidence"]["evidence_hash"] = frozen_shutdown["evidence_hash"]
    write_json(attack_binding_path, attack_binding)
    attack_binding_sha = sha256(attack_binding_path)

    attack_plan_path = attack_final / "transaction-plan.json"
    attack_plan = json.loads(attack_plan_path.read_text(encoding="utf-8"))
    original_transaction_id = str(attack_plan["transaction_id"])
    attack_plan["runtime_binding_sha256"] = attack_binding_sha
    transaction_seed = workflow.canonical_bytes(
        {
            "run_id": attack_plan["run_id"],
            "environment_manifest_sha256": attack_plan["environment_manifest_sha256"],
            "runtime_binding_sha256": attack_plan["runtime_binding_sha256"],
            "steps": attack_plan["steps"],
        }
    )
    attack_plan["transaction_id"] = (
        "finalize-" + hashlib.sha256(transaction_seed).hexdigest()[:32]
    )
    attack_plan["plan_hash"] = workflow.contract_hash_without(
        attack_plan,
        "plan_hash",
        "self-rehashed wrapper transaction plan",
    )
    write_json(attack_plan_path, attack_plan)
    attack_plan_file_sha256 = sha256(attack_plan_path)

    attack_journal_path = attack_environment / "restore-journal.jsonl"
    attack_journal = workflow.verify_journal(attack_journal_path)
    updated_journal_bindings = 0
    for entry in attack_journal:
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        if data.get("finalize_transaction_id") != original_transaction_id:
            continue
        data["finalize_transaction_id"] = attack_plan["transaction_id"]
        data["finalize_plan_hash"] = attack_plan["plan_hash"]
        data["finalize_plan_file_sha256"] = attack_plan_file_sha256
        updated_journal_bindings += 1
    assert_true(
        updated_journal_bindings > 0,
        "wrapper attack did not locate the committed finalize epoch",
    )
    write_resealed_journal(attack_journal_path, attack_journal)

    attack_recovery["runtime_binding_sha256"] = attack_binding_sha
    attack_recovery["normal_shutdown_evidence_hash"] = frozen_shutdown["evidence_hash"]
    attack_recovery["startup_certificate_projection_hash"] = attack_certificate_hash
    attack_recovery["startup_session_id"] = "self-rehashed-wrapper-session"
    attack_candidate = attack_recovery["candidate_result"]
    attack_candidate["runtime_binding_sha256"] = attack_binding_sha
    attack_candidate["normal_shutdown_evidence_hash"] = frozen_shutdown["evidence_hash"]
    attack_candidate["startup_certificate_projection_hash"] = attack_certificate_hash
    attack_candidate["startup_session_id"] = "self-rehashed-wrapper-session"
    attack_candidate["document_hash"] = workflow.contract_hash_without(
        attack_candidate,
        "document_hash",
        "self-rehashed wrapper candidate",
    )
    attack_recovery["candidate_result_hash"] = workflow.contract_canonical_hash(
        attack_candidate
    )
    attack_recovery["document_hash"] = workflow.contract_hash_without(
        attack_recovery,
        "document_hash",
        "self-rehashed wrapper recovery",
    )
    write_json(attack_recovery_path, attack_recovery)
    attack["shutdown_evidence_hash"] = frozen_shutdown["evidence_hash"]
    blocked_attack = json.loads(core(attack, "finalize", what_if=True, expected={2}).stdout)
    assert_true(
        blocked_attack.get("status") == "blocked"
        and (
            "archived startup consumption bundle replay failed"
            in str(blocked_attack.get("error"))
            or "startup certificate is not bound to the lineage origin"
            in str(blocked_attack.get("error"))
        ),
        "self-rehashed certificate/shutdown/runtime-binding/recovery wrappers bypassed archived replay: "
        + str(blocked_attack.get("error")),
    )
    return {
        "post_commit_fault": "blocked_with_nonreplay_recovery",
        "recovery_runtime_config_mismatch": "refused",
        "whatif_retry": "read_only_convergence_plan",
        "retry": "create_only_result_convergence",
        "third_call": "idempotent_same_result",
        "physical_transaction_replayed": False,
        "physical_commit_event_count": 1,
        "wrong_recovery_anchors": anchor_refusals,
        "self_rehashed_wrapper_projection": "blocked_by_archived_bundle_replay",
        "exact_result_and_recovery_shapes": "enforced",
        "candidate_result_hash": "enforced",
        "terminal_result_file_sha256": "enforced",
        "self_rehashed_extra_status_and_reconstructed_field_tamper": "refused",
        "recovery_state_candidate_coupling": "enforced",
        "fresh_post_boundary_revalidation": "required_before_result_publication",
        "create_only_revalidation_transition": "exact_and_self_hashed",
        "post_result_crash_without_recovery": "fresh_revalidated_then_idempotent",
        "implicit_source_state": workflow.IMPLICIT_POST_COMMIT_REVALIDATION_STATE,
    }


def test_finalize_fresh_revalidation_after_latch_truncation(
    base: pathlib.Path,
) -> dict[str, Any]:
    fixture = make_fixture(
        base,
        "fnc-latch",
        run_id="g0-fnc-latch",
    )
    prepare_fixture(fixture)
    simulate_runtime(fixture)
    apply_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(fixture),
            *finalize_binding_args(fixture),
            "--apply",
        ]
    )
    roots = workflow.resolve_roots(apply_args)
    original_expected = workflow._expected_finalize_result
    injected = False

    def fail_first_reconstruction(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal injected
        if not injected:
            injected = True
            raise workflow.WorkflowError("injected post-boundary reconstruction failure")
        return original_expected(*args, **kwargs)

    workflow._expected_finalize_result = fail_first_reconstruction
    try:
        try:
            with workflow.exclusive_finalize_lock(roots):
                workflow.finalize_locked(apply_args, roots)
            raise VerificationFailure("no-candidate reconstruction fault unexpectedly finalized")
        except workflow.WorkflowError:
            pass
    finally:
        workflow._expected_finalize_result = original_expected

    final_root = roots.environment_dir / "finalization"
    recovery_path = final_root / "finalize-recovery.json"
    recovery_bytes = recovery_path.read_bytes()
    recovery_payload = json.loads(recovery_bytes.decode("utf-8"))
    journal_path = roots.environment_dir / "restore-journal.jsonl"
    journal_bytes = journal_path.read_bytes()
    journal = workflow.verify_journal(journal_path)
    latch = [entry for entry in journal if entry.get("event") == "post_boundary_blocked"]
    assert_true(
        recovery_payload.get("state")
        == "post_boundary_or_publication_blocked_after_physical_commit"
        and recovery_payload.get("candidate_result") is None
        and recovery_payload.get("candidate_result_hash") is None
        and len(latch) == 1
        and latch[0] is journal[-1]
        and latch[0].get("data", {}).get("finalize_recovery_sha256")
        == sha256(recovery_path),
        "no-candidate recovery did not publish its audit journal latch",
    )

    _, manifest, manifest_hash = workflow.load_environment_manifest(apply_args, roots)
    runtime_config = workflow.validate_runtime_config_evidence(
        apply_args,
        roots,
        manifest,
        manifest_hash,
        verify_current_installation=False,
    )
    binding_path = roots.environment_dir / "runtime-binding.json"
    runtime_binding = json.loads(binding_path.read_text(encoding="utf-8"))
    candidate = original_expected(
        roots,
        final_root,
        manifest,
        manifest_hash,
        runtime_binding,
        sha256(binding_path),
        runtime_config,
    )
    attack = copy.deepcopy(recovery_payload)
    attack["state"] = "finalize_result_publication_outcome_unknown"
    attack["candidate_result"] = candidate
    attack["candidate_result_hash"] = workflow.contract_canonical_hash(candidate)
    attack["document_hash"] = workflow.contract_hash_without(
        attack,
        "document_hash",
        "phase-upgrade attack",
    )
    make_fixture_file_writable(recovery_path)
    write_json(recovery_path, attack)
    journal_lines = journal_bytes.splitlines(keepends=True)
    assert_true(len(journal_lines) >= 2, "phase-latch attack journal was unexpectedly short")
    write_bytes(journal_path, b"".join(journal_lines[:-1]))
    workflow.verify_journal(journal_path)
    result_path = final_root / "finalize-result.json"
    workflow.write_json_atomic(result_path, candidate, create_only=True)
    workflow.append_journal(
        journal_path,
        "environment_finalized_recovered",
        {
            "finalize_result_sha256": sha256(result_path),
            "state": "attacker_self_rehashed_terminal",
            "normal_shutdown_evidence_hash": candidate[
                "normal_shutdown_evidence_hash"
            ],
            "startup_consumption_evidence_hash": candidate[
                "startup_consumption_evidence_hash"
            ],
            "startup_session_id": candidate["startup_session_id"],
            "validation_process_creation_token": candidate[
                "validation_process_creation_token"
            ],
            "startup_archive_prefix_sha256": candidate[
                "startup_archive_prefix_sha256"
            ],
            "startup_archive_prefix_length": candidate[
                "startup_archive_prefix_length"
            ],
        },
    )
    before_oni = file_inventory(pathlib.Path(fixture["oni"]))
    retry_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(fixture),
            *finalize_binding_args(fixture),
            "--process-state-fixture",
            str(fixture["game"]),
            "--apply",
        ]
    )
    try:
        with workflow.exclusive_finalize_lock(roots):
            workflow.finalize_locked(retry_args, roots)
        raise VerificationFailure("truncated-latch candidate injection unexpectedly finalized")
    except workflow.WorkflowError as exc:
        blocker = str(exc)
    assert_true(
        result_path.is_file()
        and not (final_root / "revalidation-passed.json").exists()
        and not (final_root / "revalidation-comparison.json").exists()
        and file_inventory(pathlib.Path(fixture["oni"])) == before_oni
        and (
            "post-boundary" in blocker
            or "process" in blocker
            or "Oxygen Not Included must be stopped" in blocker
        ),
        "fresh post-boundary replay did not reject forged result/terminal authorization: "
        + blocker,
    )
    result_path.unlink()
    write_bytes(recovery_path, recovery_bytes)
    write_bytes(journal_path, journal_bytes)

    # Entry validation alone is insufficient: replace the pre report only
    # after the first committed-state check, and add the same pair to the real
    # TestRoot save set. A self-consistent pre/post comparison would accept it
    # unless every raw replay rebinds the pre descriptor to the manifest SHA.
    pre_path = fixture_paths(fixture)["pre"]
    original_pre_bytes = pre_path.read_bytes()
    injected_save = pathlib.Path(fixture["save_dir"]) / "InjectedAfterEntry.sav"
    injected_preview = injected_save.with_suffix(".png")
    original_restored_check = workflow._verify_committed_restored_state
    swapped_pre = False

    def swap_pre_after_first_restored_check(*args: Any, **kwargs: Any) -> None:
        nonlocal swapped_pre
        original_restored_check(*args, **kwargs)
        if swapped_pre:
            return
        swapped_pre = True
        write_bytes(injected_save, b"unexpected-save-after-entry")
        write_bytes(injected_preview, b"unexpected-preview-after-entry")
        malicious_root = pathlib.Path(fixture["case"]) / "mb"
        ps(
            CAPTURE,
            "-RunId",
            str(fixture["run_id"]),
            "-Phase",
            "pre",
            "-OniUserRoot",
            str(fixture["oni"]),
            "-ReportRoot",
            str(malicious_root),
            "-TestRoot",
            str(fixture["case"]),
            "-ProcessStateFixture",
            str(fixture["idle"]),
        )
        malicious_pre = malicious_root / str(fixture["run_id"]) / "pre-boundary.json"
        os.replace(malicious_pre, pre_path)

    workflow._verify_committed_restored_state = swap_pre_after_first_restored_check
    try:
        try:
            with workflow.exclusive_finalize_lock(roots):
                workflow.finalize_locked(apply_args, roots)
            raise VerificationFailure("post-entry pre-boundary name swap unexpectedly finalized")
        except workflow.WorkflowError as exc:
            pre_swap_blocker = str(exc)
    finally:
        workflow._verify_committed_restored_state = original_restored_check
        write_bytes(pre_path, original_pre_bytes)
        if injected_save.exists():
            injected_save.unlink()
        if injected_preview.exists():
            injected_preview.unlink()
    assert_true(
        swapped_pre
        and "immutable manifest anchor" in pre_swap_blocker
        and not (final_root / "revalidation-passed.json").exists()
        and not (final_root / "revalidation-comparison.json").exists(),
        "post-entry pre-boundary name swap was not rejected by fresh raw replay",
    )

    with workflow.exclusive_finalize_lock(roots):
        recovered = workflow.finalize_locked(apply_args, roots)
    assert_true(
        recovered.get("status") == "finalized"
        and (final_root / "revalidation-passed.json").is_file()
        and (final_root / "revalidation-comparison.json").is_file(),
        "restored recovery did not converge through a fresh passing revalidation",
    )
    return {
        "genuine_no_candidate_state": "journal_latch_audit_only",
        "truncated_latch_self_rehashed_candidate_result_terminal": "fresh_revalidation_blocked",
        "post_entry_pre_boundary_name_swap": "manifest_sha_rebind_blocked",
        "restored_retry": "fresh_revalidation_then_create_only_publication",
        "physical_transaction_replayed": False,
    }


def test_finalize_postcommit_custody_and_result_authentication(
    base: pathlib.Path,
) -> dict[str, Any]:
    race_results: dict[str, str] = {}

    def run_post_boundary_race(
        name: str,
        target: str,
        mutate: Callable[[pathlib.Path, pathlib.Path], None],
    ) -> None:
        # Keep the physical TestRoot names compact.  The descriptive attack
        # label is deliberately not repeated in the run id because the
        # startup custody bundle already contains mandatory long filenames;
        # repeating it can put an otherwise unrelated race fixture exactly on
        # the legacy Win32 MAX_PATH boundary.
        fixture_token = f"pc-{len(race_results)}"
        fixture = make_fixture(base, fixture_token, run_id=f"g0-{fixture_token}")
        prepare_fixture(fixture)
        simulate_runtime(fixture)
        args = workflow.build_parser().parse_args(
            [
                "finalize",
                *common_args(fixture),
                *finalize_binding_args(fixture),
                "--apply",
            ]
        )
        roots = workflow.resolve_roots(args)
        original_post_boundary = workflow.run_post_boundary

        def attacked_post_boundary(
            call_args: argparse.Namespace,
            call_roots: workflow.Roots,
            final_root: pathlib.Path,
            manifest: Mapping[str, Any],
        ) -> dict[str, Any]:
            comparison = original_post_boundary(
                call_args,
                call_roots,
                final_root,
                manifest,
            )
            binding = json.loads(
                (call_roots.environment_dir / "runtime-binding.json").read_text(encoding="utf-8")
            )
            archived = call_roots.environment_dir / "rb" / pathlib.Path(
                binding["startup_consumption"]["relative"]
            )
            if target == "startup_report":
                attacked = archived / startup_contract.REPORT_FILE
            else:
                custody = binding["shutdown_runtime_custody"][target]
                attacked = call_roots.environment_dir / "rb" / pathlib.Path(
                    custody["relative"]
                )
            mutate(attacked, call_roots.environment_dir)
            return comparison

        workflow.run_post_boundary = attacked_post_boundary
        try:
            try:
                with workflow.exclusive_finalize_lock(roots):
                    workflow.finalize_locked(args, roots)
                raise VerificationFailure(f"{name} post-boundary custody race was accepted")
            except workflow.WorkflowError as exc:
                blocker = str(exc)
        finally:
            workflow.run_post_boundary = original_post_boundary
        finalization = roots.environment_dir / "finalization"
        recovery_path = finalization / "finalize-recovery.json"
        result_path = finalization / "finalize-result.json"
        assert_true(
            recovery_path.is_file(),
            f"{name} left no authenticated recovery evidence after physical commit "
            f"(blocker={blocker!r})",
        )
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        recovery_state = recovery.get("state")
        recovery_candidate = recovery.get("candidate_result")
        no_candidate_blocked = (
            recovery_state
            == "post_boundary_or_publication_blocked_after_physical_commit"
            and recovery_candidate is None
            and bool(blocker)
        )
        candidate_publication_blocked = (
            recovery_state == "finalize_result_publication_outcome_unknown"
            and isinstance(recovery_candidate, dict)
            and "non-replay recovery" in blocker
        )
        assert_true(
            (no_candidate_blocked or candidate_publication_blocked)
            and not result_path.exists(),
            f"{name} did not stop after physical commit with authenticated non-replay recovery "
            f"(blocker={blocker!r}, state={recovery_state!r}, "
            f"candidate_present={isinstance(recovery_candidate, dict)})",
        )
        journal = workflow.verify_journal(roots.environment_dir / "restore-journal.jsonl")
        assert_true(
            sum(entry.get("event") == "restore_transaction_physical_committed" for entry in journal)
            == 1,
            f"{name} did not preserve the unique physical commit boundary",
        )

    def inode_swap(report: pathlib.Path, environment: pathlib.Path) -> None:
        make_fixture_file_writable(report)
        owner = environment / "startup-report-owner-before-post-boundary-swap.json"
        os.replace(report, owner)
        shutil.copy2(owner, report)

    run_post_boundary_race(
        "post-boundary-startup-inode-swap",
        "startup_report",
        inode_swap,
    )
    race_results["same_content_inode_swap_after_post_boundary"] = "blocked_before_result"

    def report_tamper(report: pathlib.Path, _environment: pathlib.Path) -> None:
        make_fixture_file_writable(report)
        write_bytes(report, report.read_bytes() + b" ")

    run_post_boundary_race(
        "post-boundary-startup-report-tamper",
        "startup_report",
        report_tamper,
    )
    race_results["startup_report_tamper_after_post_boundary"] = "blocked_before_result"

    def archive_inode_swap(archive: pathlib.Path, environment: pathlib.Path) -> None:
        owner = environment / f"{archive.parent.name}-archive-owner-before-post-boundary-swap.jsonl"
        os.replace(archive, owner)
        shutil.copy2(owner, archive)

    run_post_boundary_race(
        "post-boundary-origin-archive-inode-swap",
        "origin_archive",
        archive_inode_swap,
    )
    race_results["origin_archive_inode_swap_after_post_boundary"] = "blocked_before_result"

    def archive_tamper(archive: pathlib.Path, _environment: pathlib.Path) -> None:
        write_bytes(archive, archive.read_bytes() + b" ")

    run_post_boundary_race(
        "post-boundary-origin-archive-tamper",
        "origin_archive",
        archive_tamper,
    )
    race_results["origin_archive_tamper_after_post_boundary"] = "blocked_before_result"

    run_post_boundary_race(
        "post-boundary-activation-archive-tamper",
        "activation_reload_archive",
        archive_tamper,
    )
    race_results["activation_reload_archive_tamper_after_post_boundary"] = (
        "blocked_before_result"
    )

    run_post_boundary_race(
        "post-boundary-lineage-manifest-tamper",
        "lineage_manifest",
        archive_tamper,
    )
    race_results["lineage_manifest_tamper_after_post_boundary"] = "blocked_before_result"

    run_post_boundary_race(
        "post-boundary-final-log-tamper",
        "final_event_log",
        archive_tamper,
    )
    race_results["final_event_log_tamper_after_post_boundary"] = "blocked_before_result"

    terminal_race = make_fixture(
        base,
        "tj",
        run_id="g0-tj",
    )
    prepare_fixture(terminal_race)
    simulate_runtime(terminal_race)
    terminal_args = workflow.build_parser().parse_args(
        [
            "finalize",
            *common_args(terminal_race),
            *finalize_binding_args(terminal_race),
            "--apply",
        ]
    )
    terminal_roots = workflow.resolve_roots(terminal_args)
    original_append_journal = workflow.append_journal

    def tampering_append_journal(
        path: pathlib.Path,
        event: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        entry = original_append_journal(path, event, data)
        if event == "environment_finalized":
            binding = json.loads(
                (terminal_roots.environment_dir / "runtime-binding.json").read_text(
                    encoding="utf-8"
                )
            )
            relative = pathlib.Path(
                binding["shutdown_runtime_custody"]["origin_archive"]["relative"]
            )
            archived_origin = terminal_roots.environment_dir / "rb" / relative
            write_bytes(archived_origin, archived_origin.read_bytes() + b" ")
        return entry

    workflow.append_journal = tampering_append_journal
    try:
        try:
            with workflow.exclusive_finalize_lock(terminal_roots):
                workflow.finalize_locked(terminal_args, terminal_roots)
            raise VerificationFailure(
                "terminal-journal custody race returned a false finalized outcome"
            )
        except workflow.WorkflowError as exc:
            terminal_blocker = str(exc)
    finally:
        workflow.append_journal = original_append_journal
    terminal_finalization = terminal_roots.environment_dir / "finalization"
    terminal_recovery_path = terminal_finalization / "finalize-recovery.json"
    assert_true(
        terminal_recovery_path.is_file(),
        "terminal-journal custody race left no authenticated recovery evidence "
        f"(blocker={terminal_blocker!r})",
    )
    terminal_recovery = json.loads(
        terminal_recovery_path.read_text(encoding="utf-8")
    )
    assert_true(
        "outcome_unknown" in terminal_blocker
        and terminal_recovery.get("state")
        == "finalize_result_published_journal_outcome_unknown"
        and (terminal_finalization / "finalize-result.json").is_file(),
        "post-terminal custody mutation did not remain blocked with explicit recovery",
    )
    race_results["terminal_journal_then_origin_tamper"] = "blocked_outcome_unknown"

    result_fixture = make_fixture(
        base,
        "ra",
        run_id="g0-ra",
    )
    prepare_fixture(result_fixture)
    simulate_runtime(result_fixture)
    published = json.loads(core(result_fixture, "finalize", apply=True).stdout)
    result_path = (
        fixture_paths(result_fixture)["environment"]
        / "finalization"
        / "finalize-result.json"
    )
    original_result = result_path.read_bytes()

    def assert_result_attack_refused(
        label: str,
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        document = json.loads(original_result.decode("utf-8"))
        mutate(document)
        document["document_hash"] = workflow.contract_hash_without(
            document,
            "document_hash",
            label,
        )
        write_json(result_path, document)
        try:
            blocked = json.loads(
                core(result_fixture, "finalize", what_if=True, expected={2}).stdout
            )
            assert_true(blocked.get("status") == "blocked", f"{label} was accepted")
        finally:
            write_bytes(result_path, original_result)

    assert_result_attack_refused(
        "self-rehashed finalize-result extra key",
        lambda document: document.update({"unexpected": "field"}),
    )
    assert_result_attack_refused(
        "self-rehashed finalize-result post_boundary_status",
        lambda document: document.update({"post_boundary_status": "skipped"}),
    )
    compact_result = json.dumps(
        json.loads(original_result.decode("utf-8")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert_true(compact_result != original_result, "terminal raw-hash attack did not change bytes")
    write_bytes(result_path, compact_result)
    try:
        blocked = json.loads(
            core(result_fixture, "finalize", what_if=True, expected={2}).stdout
        )
        assert_true(
            blocked.get("status") == "blocked",
            "terminal journal raw result hash mismatch was accepted",
        )
    finally:
        write_bytes(result_path, original_result)
    replay = json.loads(core(result_fixture, "finalize", apply=True).stdout)
    assert_true(
        replay == published and result_path.read_bytes() == original_result,
        "restored finalize result did not remain idempotent after authentication attacks",
    )
    return {
        **race_results,
        "result_extra_key": "refused",
        "result_post_boundary_status": "refused",
        "terminal_journal_finalize_result_sha256": "enforced",
        "restored_result_replay": "idempotent",
    }


def test_guards_and_partial_rollback(base: pathlib.Path) -> dict[str, Any]:
    process_fixture = make_fixture(base, "process-guard", run_id="g0-process-guard")
    prepare_fixture(process_fixture)
    simulate_runtime(process_fixture)
    before_process = file_inventory(pathlib.Path(process_fixture["oni"]))
    process_args = ["--process-state-fixture", str(process_fixture["game"])]
    # Replace the common idle fixture by appending the same option is ambiguous,
    # so build the command explicitly and alter the existing value.
    command = [sys.executable, str(CORE), "finalize", *common_args(process_fixture)]
    fixture_index = command.index("--process-state-fixture") + 1
    command[fixture_index] = str(process_fixture["game"])
    bridge = pathlib.Path(process_fixture["oni"]) / "ai-bridge"
    command.extend(
        [
            "--manifest",
            str(fixture_paths(process_fixture)["manifest"]),
            "--lineage-manifest",
            str(bridge / "evidence" / "save-lineage" / str(process_fixture["run_id"]) / "manifest.json"),
            "--priority-pair-report",
            str(bridge / "evidence" / "save-lineage" / str(process_fixture["run_id"]) / "priority-pair-finalization.json"),
            "--priority-pair-report-hash",
            str(process_fixture["pair_report_hash"]),
            "--shutdown-evidence",
            str(process_fixture["shutdown_evidence_path"]),
            "--shutdown-evidence-hash",
            str(process_fixture["shutdown_evidence_hash"]),
            "--startup-consumption-bundle",
            str(process_fixture["startup_consumption_bundle"]),
            "--startup-consumption-evidence-hash",
            str(process_fixture["startup_consumption_evidence_hash"]),
            "--validation-process-id",
            str(process_fixture["validation_process_id"]),
            "--runtime-config-evidence",
            str(process_fixture["runtime_config_evidence_path"]),
            "--expected-runtime-config-evidence-sha256",
            str(process_fixture["runtime_config_evidence_sha256"]),
            "--expected-startup-binding-sha256",
            str(process_fixture["startup_binding_sha256"]),
            "--runtime-bootstrap-result",
            str(process_fixture["runtime_bootstrap_result"]),
            "--expected-runtime-bootstrap-result-sha256",
            str(process_fixture["expected_runtime_bootstrap_result_sha256"]),
            "--release-dll",
            str(process_fixture["release_dll"]),
            "--expected-release-dll-sha256",
            str(process_fixture["expected_release_dll_sha256"]),
            "--game-managed-dir",
            str(process_fixture["game_managed_dir"]),
            "--test-managed-assembly-csharp-sha256",
            str(process_fixture["managed_csharp_sha256"]),
            "--test-managed-assembly-csharp-firstpass-sha256",
            str(process_fixture["managed_firstpass_sha256"]),
            "--forward-step-id",
            "priority_forward",
            "--inverse-step-id",
            "priority_inverse",
            "--command-id",
            "cmd-priority-forward-fixture",
            "--command-id",
            "cmd-priority-inverse-fixture",
            "--command-bundle-hash",
            str(process_fixture["forward_bundle_hash"]),
            "--command-bundle-hash",
            str(process_fixture["inverse_bundle_hash"]),
            "--apply",
        ]
    )
    blocked = json.loads(run(command, expected={2}).stdout)
    assert_true(blocked.get("status") == "blocked", "active process fixture was accepted")
    assert_true(file_inventory(pathlib.Path(process_fixture["oni"])) == before_process, "process guard mutated fixture")

    rollback_fixture = make_fixture(base, "partial-rollback", run_id="g0-partial-rollback")
    prepare_fixture(rollback_fixture)
    rollback_runtime = simulate_runtime(rollback_fixture)
    rollback_bundle = pathlib.Path(rollback_fixture["startup_consumption_bundle"])
    rollback_custody_before = workflow._startup_bundle_custody_snapshot(
        rollback_bundle,
        rollback_runtime["bridge"],
    )
    rollback_roots = workflow.resolve_roots(
        workflow.build_parser().parse_args(["prepare", *common_args(rollback_fixture), "--what-if"])
    )
    with workflow.exclusive_finalize_lock(rollback_roots):
        blocked = json.loads(core(rollback_fixture, "finalize", apply=True, expected={2}).stdout)
        assert_true(blocked.get("status") == "blocked", "concurrent finalize lock was accepted")
    runtime_before = file_inventory(pathlib.Path(rollback_fixture["oni"]))
    blocked = json.loads(
        core(
            rollback_fixture,
            "finalize",
            "--test-fail-after-step",
            "16",
            apply=True,
            expected={2},
        ).stdout
    )
    assert_true(blocked.get("status") == "blocked", "fault injection unexpectedly succeeded")
    runtime_after = file_inventory(pathlib.Path(rollback_fixture["oni"]))
    if runtime_after != runtime_before:
        added = sorted(runtime_after.keys() - runtime_before.keys())
        removed = sorted(runtime_before.keys() - runtime_after.keys())
        changed = sorted(
            key
            for key in runtime_before.keys() & runtime_after.keys()
            if runtime_before[key] != runtime_after[key]
        )
        raise VerificationFailure(
            "partial failure rollback did not restore runtime files exactly: "
            f"added={added}, removed={removed}, changed={changed}"
        )
    runtime_transaction_artifacts = [
        path
        for path in pathlib.Path(rollback_fixture["oni"]).rglob("*")
        if any(
            marker in path.name.casefold()
            for marker in (".tmp", ".claim", "rollback-claim", "transaction-recovery")
        )
    ]
    assert_true(
        not runtime_transaction_artifacts,
        "partial failure left a transaction artifact inside oni-user",
    )
    rollback_relative = rollback_bundle.relative_to(rollback_runtime["bridge"])
    assert_true(
        rollback_bundle.is_dir()
        and not (fixture_paths(rollback_fixture)["environment"] / "rb" / rollback_relative).exists()
        and workflow._startup_bundle_custody_snapshot(
            rollback_bundle,
            rollback_runtime["bridge"],
        )
        == rollback_custody_before,
        "bridge-move rollback did not restore unique startup bundle identity/bytes/mtime custody",
    )
    journal = fixture_paths(rollback_fixture)["environment"] / "restore-journal.jsonl"
    events = [json.loads(line)["event"] for line in journal.read_text(encoding="utf-8").splitlines()]
    assert_true("restore_rollback_complete" in events and "restore_rollback_failed" not in events, "rollback journal is incomplete")
    result = json.loads(core(rollback_fixture, "finalize", apply=True).stdout)
    assert_true(result.get("status") == "finalized", "finalize did not recover after rollback")
    verified_entries = workflow.verify_journal(journal)
    finalize_attempt_ids = [
        entry["data"]["finalize_attempt_id"]
        for entry in verified_entries
        if entry.get("event") == "restore_transaction_started"
        and isinstance(entry.get("data"), dict)
        and isinstance(entry["data"].get("finalize_attempt_id"), str)
    ]
    assert_true(
        len(finalize_attempt_ids) == 2
        and len(set(finalize_attempt_ids)) == 2,
        "clean rollback did not open a distinct retry attempt epoch",
    )
    assert_true(
        sum(entry.get("event") == "restore_rollback_complete" for entry in verified_entries) == 1
        and sum(
            entry.get("event") == "restore_transaction_physical_committed"
            for entry in verified_entries
        )
        == 1,
        "retry journal did not preserve one closed rollback epoch and one commit epoch",
    )
    plan_path = fixture_paths(rollback_fixture)["environment"] / "finalization" / "transaction-plan.json"
    plan = workflow._validate_finalize_transaction_plan(
        json.loads(plan_path.read_text(encoding="utf-8")),
        roots=rollback_roots,
        manifest_hash=sha256(
            fixture_paths(rollback_fixture)["environment"] / "environment-manifest.json"
        ),
        runtime_binding_sha256=sha256(
            fixture_paths(rollback_fixture)["environment"] / "runtime-binding.json"
        ),
    )
    assert_true(
        workflow._classify_finalize_transaction_journal(
            rollback_roots,
            plan,
            sha256(plan_path),
        )
        == "physical_committed",
        "retry journal did not classify the final attempt as uniquely committed",
    )
    assert_true(
        not (fixture_paths(rollback_fixture)["environment"] / "finalization" / "transaction-recovery.json").exists(),
        "clean rollback left an outcome_unknown transaction latch",
    )
    return {
        "process_guard": "blocked",
        "concurrent_finalize": "exclusive_lock_refused",
        "fault_after_step": 16,
        "rollback": "exact_runtime_files_restored",
        "oni_user_added_removed_changed": [0, 0, 0],
        "oni_user_transaction_artifact_count": 0,
        "environment_evidence_incremental": True,
        "exception_only_descriptor_retry_limit": 1,
        "observed_identity_or_content_mismatch_retry": 0,
        "rollback_claim_names": "compact_unique_legacy_win32_safe",
        "startup_bundle_bridge_move_rollback": "unique_identity_bytes_mtime_restored",
        "journal_hash_chain": "passed",
        "journal_attempt_epochs": 2,
        "clean_rollback_epoch_closed": True,
        "unique_committed_retry_epoch": True,
        "retry_after_rollback": "passed",
    }


def abort_namespace(
    fixture: dict[str, pathlib.Path | str],
    *extra: str,
    apply: bool = False,
) -> Any:
    arguments = ["abort", *common_args(fixture), *abort_binding_args(fixture), *extra]
    arguments.append("--apply" if apply else "--what-if")
    return workflow.build_parser().parse_args(arguments)


def assert_abort_blocked_without_writes(
    fixture: dict[str, pathlib.Path | str],
    *extra: str,
) -> dict[str, Any]:
    case = pathlib.Path(fixture["case"])
    before = file_inventory(case)
    completed = core(fixture, "abort", *extra, apply=True, expected={2})
    payload = json.loads(completed.stdout)
    assert_true(payload.get("status") == "blocked", "unsafe abort preflight was accepted")
    assert_true(file_inventory(case) == before, "blocked abort changed TestRoot state")
    assert_true(
        not (fixture_paths(fixture)["environment"] / "abort" / "preflight.json").exists(),
        "blocked abort published preflight evidence",
    )
    return payload


def test_abort_success_partial_and_replay(base: pathlib.Path) -> dict[str, Any]:
    exact = make_fixture(base, "abort-success-exact", run_id="g0-abort-success-exact")
    exact_baseline = file_inventory(pathlib.Path(exact["oni"]))
    prepare_fixture(exact)
    prepare_configured_runtime(exact, add_diagnostics=True)
    case_before_plan = file_inventory(pathlib.Path(exact["case"]))
    exact_plan = json.loads(core(exact, "abort", what_if=True).stdout)
    assert_true(
        exact_plan.get("status") == "would_abort"
        and exact_plan.get("configuration_state") == "configured_exact"
        and exact_plan.get("write_count") == 0,
        "configured abort WhatIf plan is invalid",
    )
    assert_true(file_inventory(pathlib.Path(exact["case"])) == case_before_plan, "abort WhatIf wrote state")
    exact_result = json.loads(core(exact, "abort", apply=True).stdout)
    assert_true(exact_result.get("status") == "aborted", "configured abort did not complete")
    assert_true(file_inventory(pathlib.Path(exact["oni"])) == exact_baseline, "configured abort was not exact")
    abort_root = fixture_paths(exact)["environment"] / "abort"
    assert_true(
        (abort_root / "rb" / "sessions" / "waiting" / "loader-failure.json").is_file(),
        "loader failure diagnostic was not archived",
    )
    result_hash = sha256(abort_root / "abort-result.json")
    replay = json.loads(core(exact, "abort", apply=True).stdout)
    assert_true(
        replay == exact_result and sha256(abort_root / "abort-result.json") == result_hash,
        "completed abort replayed or changed its immutable result",
    )
    journal_events = [
        json.loads(line)["event"]
        for line in (fixture_paths(exact)["environment"] / "restore-journal.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert_true(
        journal_events.count("restore_transaction_physical_committed") == 1,
        "abort replay repeated the physical restore transaction",
    )

    partial = make_fixture(base, "abort-success-partial", run_id="g0-abort-success-partial")
    partial_baseline = file_inventory(pathlib.Path(partial["oni"]))
    prepare_fixture(partial)
    prepare_configured_runtime(partial)
    write_bytes(
        pathlib.Path(partial["oni"]) / "kplayerprefs.yaml",
        (
            "strings:\r\n"
            f"  SaveFilenameKey/: {fixture_paths(partial)['loader']}\r\n"
            "ints:\r\n  SESSION_ID: 1\r\n"
        ).encode("utf-8"),
    )
    partial_plan = json.loads(core(partial, "abort", what_if=True).stdout)
    assert_true(
        partial_plan.get("configuration_state") == "partial_or_startup_loader_drift",
        "post-AutoResume loader drift was not explicitly classified",
    )
    partial_result = json.loads(core(partial, "abort", apply=True).stdout)
    assert_true(
        partial_result.get("status") == "aborted"
        and partial_result.get("configuration_state") == "partial_or_startup_loader_drift",
        "loader-drift abort did not preserve its classification",
    )
    assert_true(file_inventory(pathlib.Path(partial["oni"])) == partial_baseline, "loader-drift abort was not exact")

    absent_archive = make_fixture(
        base,
        "abort-archive-absent",
        run_id="g0-abort-ar-absent",
    )
    absent_archive_root = pathlib.Path(absent_archive["oni"]) / "ai-bridge-archives"
    shutil.rmtree(absent_archive_root)
    absent_baseline = file_inventory(pathlib.Path(absent_archive["oni"]))
    prepare_fixture(absent_archive)
    prepare_configured_runtime(absent_archive)
    absent_result = json.loads(core(absent_archive, "abort", apply=True).stdout)
    assert_true(
        absent_result.get("status") == "aborted"
        and file_inventory(pathlib.Path(absent_archive["oni"])) == absent_baseline
        and not absent_archive_root.exists(),
        "abort did not transactionally remove the bootstrap-created archive root",
    )
    return {
        "whatif": "zero_write",
        "configured_exact": "restored",
        "loader_failure_diagnostics": "archived",
        "partial_or_startup_loader_drift": "classified_and_restored",
        "original_archive_root_absent": "created_root_archived_then_removed",
        "replay": "idempotent_nonreplay",
        "physical_commit_count": 1,
        "active_parent_finalize_lock": "authentic_abort_apply_accepted",
    }


def test_abort_session_action_refusals(base: pathlib.Path) -> dict[str, Any]:
    mutations: list[tuple[str, Callable[[dict[str, pathlib.Path | str]], None]]] = [
        (
            "session_started",
            lambda fixture: write_jsonl(
                pathlib.Path(fixture["oni"]) / "ai-bridge" / "state.jsonl",
                [{"schema": "oni.event.v2", "event": "session_started", "session_id": "session-x"}],
            ),
        ),
        (
            "snapshot",
            lambda fixture: write_json(
                pathlib.Path(fixture["oni"]) / "ai-bridge" / "latest_state.json",
                {"schema": "oni.state.v2", "event": "state_snapshot", "session_id": "session-x"},
            ),
        ),
        (
            "command_stage",
            lambda fixture: write_json(
                pathlib.Path(fixture["oni"]) / "ai-bridge" / "commands" / "done" / "cmd-x.json",
                {"id": "cmd-x", "status": "done"},
            ),
        ),
        (
            "validation_control",
            lambda fixture: write_json(
                pathlib.Path(fixture["oni"]) / "ai-bridge" / "validation-controls" / "done" / "ctl-x.json",
                {"id": "ctl-x", "status": "done"},
            ),
        ),
        (
            "approval_bundle",
            lambda fixture: write_json(
                pathlib.Path(fixture["oni"])
                / "ai-bridge"
                / "confirmed-command-evidence"
                / ("a" * 64)
                / "approval-bundle.json",
                {"command_id": "cmd-x"},
            ),
        ),
        (
            "lineage_freeze",
            lambda fixture: write_json(
                pathlib.Path(fixture["oni"])
                / "ai-bridge"
                / "evidence"
                / "save-lineage"
                / str(fixture["run_id"])
                / "priority-command-freezes"
                / "forward.json",
                {"step_id": "forward"},
            ),
        ),
        (
            "startup_consumption_certificate_bundle",
            lambda fixture: write_json(
                pathlib.Path(fixture["oni"])
                / "ai-bridge"
                / "evidence"
                / "save-lineage"
                / str(fixture["run_id"])
                / shutdown_contract.STARTUP_BUNDLE_DIRECTORY
                / startup_contract.REPORT_FILE,
                {"schema": startup_contract.EVIDENCE_SCHEMA, "status": "passed"},
            ),
        ),
        (
            "outcomes",
            lambda fixture: write_json(
                pathlib.Path(fixture["oni"])
                / "ai-bridge"
                / "sessions"
                / "session-x"
                / "action-outcomes.json",
                {"schema": "oni.action-outcomes.v1"},
            ),
        ),
        (
            "ack",
            lambda fixture: write_jsonl(
                pathlib.Path(fixture["oni"]) / "ai-bridge" / "acks.jsonl",
                [{"event": "command_ack", "command_id": "cmd-x", "session_id": "session-x"}],
            ),
        ),
        (
            "save_write",
            lambda fixture: write_jsonl(
                pathlib.Path(fixture["oni"]) / "ai-bridge" / "state.jsonl",
                [{"event": "save_write_completed", "session_id": None}],
            ),
        ),
    ]
    blocked: list[str] = []
    for index, (name, mutation) in enumerate(mutations):
        fixture = make_fixture(base, f"abort-forbidden-{index}", run_id=f"g0-abort-forbidden-{index}")
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)
        mutation(fixture)
        assert_abort_blocked_without_writes(fixture)
        blocked.append(name)
    return {"blocked_zero_write": blocked, "count": len(blocked)}


def test_abort_anchor_identity_and_lock_refusals(base: pathlib.Path) -> dict[str, Any]:
    overrides = [
        ("environment_manifest", "--expected-environment-manifest-sha256"),
        ("runtime_config", "--expected-runtime-config-evidence-sha256"),
        ("startup_binding", "--expected-startup-binding-sha256"),
        (
            "runtime_bootstrap_result",
            "--expected-runtime-bootstrap-result-sha256",
        ),
        ("release", "--expected-release-dll-sha256"),
        ("managed", "--test-managed-assembly-csharp-sha256"),
        ("managed_firstpass", "--test-managed-assembly-csharp-firstpass-sha256"),
    ]
    results: dict[str, Any] = {}
    for index, (name, option) in enumerate(overrides):
        fixture = make_fixture(base, f"abort-anchor-{index}", run_id=f"g0-abort-anchor-{index}")
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)
        assert_abort_blocked_without_writes(fixture, option, "sha256:" + "0" * 64)
        results[name] = "blocked_zero_write"

    policy = make_fixture(base, "abort-policy-drift", run_id="g0-abort-policy-drift")
    prepare_fixture(policy)
    prepare_configured_runtime(policy)
    changed_policy = disabled_policy()
    changed_policy["generated_at"] = "2026-08-31T00:00:00+00:00"
    write_json(pathlib.Path(policy["oni"]) / "ai-bridge" / "action-policy.json", changed_policy)
    assert_abort_blocked_without_writes(policy)
    results["initial_policy_raw_drift"] = "blocked_zero_write"

    for index, managed_name in enumerate(runtime_config_contract.MANAGED_ASSEMBLY_NAMES):
        hardlink = make_fixture(
            base,
            f"abort-managed-hardlink-{index}",
            run_id=f"g0-abort-managed-hardlink-{index}",
        )
        prepare_fixture(hardlink)
        prepare_configured_runtime(hardlink)
        managed_file = pathlib.Path(hardlink["game_managed_dir"]) / managed_name
        os.link(managed_file, managed_file.with_name(f"{managed_name}.alias.dll"))
        assert_abort_blocked_without_writes(hardlink)
        results[f"managed_hardlink_{index}"] = "blocked_zero_write"

    wrong_dir = make_fixture(base, "abort-managed-wrong-dir", run_id="g0-abort-managed-wrong-dir")
    prepare_fixture(wrong_dir)
    prepare_configured_runtime(wrong_dir)
    alternate = pathlib.Path(wrong_dir["case"]) / "alternate-game" / "OxygenNotIncluded_Data" / "Managed"
    alternate.mkdir(parents=True)
    for managed_name in runtime_config_contract.MANAGED_ASSEMBLY_NAMES:
        shutil.copy2(pathlib.Path(wrong_dir["game_managed_dir"]) / managed_name, alternate / managed_name)
    assert_abort_blocked_without_writes(wrong_dir, "--game-managed-dir", str(alternate))
    results["wrong_game_managed_dir"] = "blocked_zero_write"

    parent_swap = make_fixture(base, "abort-managed-parent-swap", run_id="g0-abort-managed-parent-swap")
    prepare_fixture(parent_swap)
    prepare_configured_runtime(parent_swap)
    managed_dir = pathlib.Path(parent_swap["game_managed_dir"])
    moved_dir = managed_dir.parent / "Managed-original"
    fired = False
    swapped = False

    def swap_parent(stage: str, path: pathlib.Path) -> None:
        nonlocal fired, swapped
        if stage != "__before_final_revalidation__" or fired:
            return
        fired = True
        os.replace(managed_dir, moved_dir)
        swapped = True
        managed_dir.mkdir()
        for name in runtime_config_contract.MANAGED_ASSEMBLY_NAMES:
            shutil.copy2(moved_dir / name, managed_dir / name)

    prior_hook = runtime_config_contract._TEST_MANAGED_ASSEMBLY_HOOK
    runtime_config_contract._TEST_MANAGED_ASSEMBLY_HOOK = swap_parent
    try:
        args = abort_namespace(parent_swap)
        roots = workflow.resolve_roots(args)
        before = file_inventory(pathlib.Path(parent_swap["case"]))
        try:
            workflow.abort_locked(args, roots)
        except workflow.WorkflowError:
            pass
        else:
            raise VerificationFailure("Managed parent identity swap was accepted")
        assert_true(fired, "Managed parent swap hook did not fire")
    finally:
        runtime_config_contract._TEST_MANAGED_ASSEMBLY_HOOK = prior_hook
        if swapped and managed_dir.exists():
            shutil.rmtree(managed_dir)
        if swapped and moved_dir.exists():
            os.replace(moved_dir, managed_dir)
    assert_true(
        file_inventory(pathlib.Path(parent_swap["case"])) == before,
        "Managed parent swap refusal changed state after fixture restoration",
    )
    results["managed_parent_swap"] = "os_or_descriptor_ancestry_refused"

    locked = make_fixture(base, "abort-lock", run_id="g0-abort-lock")
    prepare_fixture(locked)
    prepare_configured_runtime(locked)
    args = abort_namespace(locked, apply=True)
    roots = workflow.resolve_roots(args)
    before_locked = file_inventory(pathlib.Path(locked["case"]))
    with workflow.exclusive_finalize_lock(roots):
        blocked = json.loads(core(locked, "abort", apply=True, expected={2}).stdout)
        assert_true(blocked.get("status") == "blocked", "shared finalize/abort lock was not exclusive")
    assert_true(file_inventory(pathlib.Path(locked["case"])) == before_locked, "lock refusal changed state")
    results["shared_lock"] = "blocked_zero_write"

    process = make_fixture(base, "abort-process", run_id="g0-abort-process")
    prepare_fixture(process)
    prepare_configured_runtime(process)
    assert_abort_blocked_without_writes(
        process, "--process-state-fixture", str(process["game"])
    )
    results["active_process"] = "blocked_zero_write"

    confirmation = make_fixture(base, "abort-confirmation", run_id="g0-abort-confirmation")
    prepare_fixture(confirmation)
    prepare_configured_runtime(confirmation)
    assert_abort_blocked_without_writes(
        confirmation, "--confirm-live-run-id", "wrong-run"
    )
    results["wrong_confirmation"] = "blocked_zero_write"
    return results


def test_abort_all_step_rollback_and_external_replacement(base: pathlib.Path) -> dict[str, Any]:
    # Fixture has three present config files (mods/config.json is intentionally
    # absent): loader 4 + configs 6 + Probe 2 + Bridge 2 + archive mtime 1.
    step_count = 15
    for step in range(1, step_count + 1):
        fixture = make_fixture(base, f"abort-rollback-{step}", run_id=f"g0-abort-rollback-{step}")
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)
        runtime_before = file_inventory(pathlib.Path(fixture["oni"]))
        blocked = json.loads(
            core(
                fixture,
                "abort",
                "--test-abort-fail-after-step",
                str(step),
                apply=True,
                expected={2},
            ).stdout
        )
        assert_true(blocked.get("status") == "blocked", f"abort fault step {step} unexpectedly passed")
        assert_true(
            file_inventory(pathlib.Path(fixture["oni"])) == runtime_before,
            f"abort fault step {step} did not rollback exactly",
        )
        assert_true(
            not (fixture_paths(fixture)["environment"] / ".finalize.lock").exists(),
            f"abort fault step {step} left the shared lock",
        )
        recovered = json.loads(core(fixture, "abort", apply=True).stdout)
        assert_true(recovered.get("status") == "aborted", f"abort fault step {step} did not retry")

    replacement = make_fixture(base, "abort-external-replacement", run_id="g0-abort-external-replacement")
    prepare_fixture(replacement)
    prepare_configured_runtime(replacement)
    args = abort_namespace(replacement, apply=True)
    roots = workflow.resolve_roots(args)
    abort_root = fixture_paths(replacement)["environment"] / "abort"
    preserved_owned = abort_root / "external-owned-loader"
    injected = False

    def replace_after_move(label: str, target: pathlib.Path) -> None:
        nonlocal injected
        if label != "abort_archive_loader_save" or injected:
            return
        injected = True
        os.replace(target, preserved_owned)
        write_bytes(target, b"external-loader-replacement")

    try:
        with workflow.exclusive_finalize_lock(roots):
            workflow.abort_locked(
                args,
                roots,
                transaction_after_physical_hook=replace_after_move,
            )
    except workflow.WorkflowError:
        pass
    else:
        raise VerificationFailure("abort external replacement race unexpectedly succeeded")
    target = abort_root / "x" / "ls"
    assert_true(
        injected
        and target.read_bytes() == b"external-loader-replacement"
        and preserved_owned.is_file()
        and (abort_root / "transaction-recovery.json").is_file(),
        "abort rollback overwrote or lost an external replacement",
    )
    return {
        "fault_steps_rolled_back_and_retried": step_count,
        "external_replacement": "retained_with_outcome_unknown_recovery",
    }


def _invoke_abort_crash(
    fixture: dict[str, pathlib.Path | str],
    **hooks: Any,
) -> None:
    args = abort_namespace(fixture, apply=True)
    roots = workflow.resolve_roots(args)
    try:
        with workflow.exclusive_finalize_lock(roots):
            workflow.abort_locked(args, roots, **hooks)
    except SystemExit:
        return
    raise VerificationFailure("process-exit fault did not interrupt abort")


def _converge_after_abort_crash(fixture: dict[str, pathlib.Path | str]) -> list[str]:
    statuses: list[str] = []
    try:
        first = json.loads(core(fixture, "abort", apply=True).stdout)
    except VerificationFailure as exc:
        raise VerificationFailure(
            f"{fixture['run_id']} first crash-recovery invocation failed: {exc}"
        ) from exc
    statuses.append(str(first.get("status")))
    if first.get("status") == "recovered_to_pre_abort":
        try:
            second = json.loads(core(fixture, "abort", apply=True).stdout)
        except VerificationFailure as exc:
            raise VerificationFailure(
                f"{fixture['run_id']} second crash-recovery invocation failed: {exc}"
            ) from exc
        statuses.append(str(second.get("status")))
    assert_true(statuses[-1] == "aborted", "abort crash recovery did not converge")
    return statuses


def test_abort_durable_crash_recovery_and_result_authentication(base: pathlib.Path) -> dict[str, Any]:
    crash_boundaries: dict[str, int] = {}
    representative_steps = (1, 8, 15)
    for boundary in representative_steps:
        fixture = make_fixture(
            base,
            f"abort-crash-intent-{boundary}",
            run_id=f"g0-abort-crash-intent-{boundary}",
        )
        before = file_inventory(pathlib.Path(fixture["oni"]))
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)
        calls = 0

        def crash_before_publish(_label: str, _target: pathlib.Path) -> None:
            nonlocal calls
            calls += 1
            if calls == boundary:
                raise SystemExit("intent-boundary crash")

        _invoke_abort_crash(fixture, transaction_before_publish_hook=crash_before_publish)
        statuses = _converge_after_abort_crash(fixture)
        assert_true(
            file_inventory(pathlib.Path(fixture["oni"])) == before,
            f"intent-boundary crash {boundary} did not converge to exact original state",
        )
        crash_boundaries[f"intent_{boundary}"] = len(statuses)

    for boundary in range(1, 16):
        fixture = make_fixture(
            base,
            f"abort-crash-physical-{boundary}",
            run_id=f"g0-abort-crash-physical-{boundary}",
        )
        before = file_inventory(pathlib.Path(fixture["oni"]))
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)
        calls = 0

        def crash_after_physical(_label: str, _target: pathlib.Path) -> None:
            nonlocal calls
            calls += 1
            if calls == boundary:
                raise SystemExit("post-rename crash")

        _invoke_abort_crash(fixture, transaction_after_physical_hook=crash_after_physical)
        statuses = _converge_after_abort_crash(fixture)
        assert_true(
            "recovered_to_pre_abort" in statuses
            and file_inventory(pathlib.Path(fixture["oni"])) == before,
            f"physical-boundary crash {boundary} was not durably recovered",
        )
        crash_boundaries[f"physical_{boundary}"] = len(statuses)

    for boundary in representative_steps:
        fixture = make_fixture(
            base,
            f"abort-crash-completed-{boundary}",
            run_id=f"g0-abort-crash-completed-{boundary}",
        )
        before = file_inventory(pathlib.Path(fixture["oni"]))
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)

        def crash_after_completed(step: int, _label: str) -> None:
            if step == boundary:
                raise SystemExit("completed-boundary crash")

        _invoke_abort_crash(fixture, transaction_after_step_hook=crash_after_completed)
        statuses = _converge_after_abort_crash(fixture)
        assert_true(
            "recovered_to_pre_abort" in statuses
            and file_inventory(pathlib.Path(fixture["oni"])) == before,
            f"completed-boundary crash {boundary} was not durably recovered",
        )
        crash_boundaries[f"completed_{boundary}"] = len(statuses)

    committed = make_fixture(base, "abort-crash-commit", run_id="g0-abort-crash-commit")
    committed_before = file_inventory(pathlib.Path(committed["oni"]))
    prepare_fixture(committed)
    prepare_configured_runtime(committed)
    _invoke_abort_crash(
        committed,
        transaction_after_commit_hook=lambda _plan: (_ for _ in ()).throw(SystemExit("commit crash")),
    )
    commit_statuses = _converge_after_abort_crash(committed)
    assert_true(
        commit_statuses == ["aborted"]
        and file_inventory(pathlib.Path(committed["oni"])) == committed_before,
        "post-commit crash replayed or failed to converge the physical restore",
    )

    precommit = make_fixture(base, "abort-crash-precommit", run_id="g0-abort-crash-precommit")
    precommit_before = file_inventory(pathlib.Path(precommit["oni"]))
    prepare_fixture(precommit)
    prepare_configured_runtime(precommit)
    _invoke_abort_crash(
        precommit,
        transaction_before_commit_hook=lambda _plan: (_ for _ in ()).throw(SystemExit("precommit crash")),
    )
    precommit_statuses = _converge_after_abort_crash(precommit)
    assert_true(
        precommit_statuses == ["recovered_to_pre_abort", "aborted"]
        and file_inventory(pathlib.Path(precommit["oni"])) == precommit_before,
        "durable result-intent boundary crash did not recover then converge exactly",
    )

    foreign = make_fixture(base, "abort-foreign-result", run_id="g0-abort-foreign-result")
    prepare_fixture(foreign)
    prepare_configured_runtime(foreign)
    _invoke_abort_crash(
        foreign,
        transaction_before_publish_hook=lambda _label, _target: (_ for _ in ()).throw(
            SystemExit("precommit crash")
        ),
    )
    foreign_result = fixture_paths(foreign)["environment"] / "abort" / "abort-result.json"
    write_json(
        foreign_result,
        {
            "schema": workflow.ABORT_RESULT_SCHEMA,
            "status": "aborted",
            "run_id": foreign["run_id"],
            "physical_restore_committed": True,
        },
    )
    blocked = json.loads(core(foreign, "abort", apply=True, expected={2}).stdout)
    assert_true(
        blocked.get("status") == "blocked" and "physical-commit" in str(blocked.get("error")),
        "foreign precommit abort result bypassed durable commit authentication",
    )

    tampered = make_fixture(base, "abort-recovery-tamper", run_id="g0-abort-recovery-tamper")
    prepare_fixture(tampered)
    prepare_configured_runtime(tampered)
    core(
        tampered,
        "abort",
        "--test-abort-result-fail-after-verify",
        apply=True,
        expected={2},
    )
    recovery_path = fixture_paths(tampered)["environment"] / "abort" / "abort-recovery.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    recovery["candidate_result"]["unexpected"] = True
    recovery["candidate_result"]["document_hash"] = workflow.contract_hash_without(
        recovery["candidate_result"], "document_hash", "tampered candidate"
    )
    recovery["candidate_result_hash"] = workflow.contract_canonical_hash(recovery["candidate_result"])
    recovery["document_hash"] = workflow.contract_hash_without(recovery, "document_hash", "tampered recovery")
    write_json(recovery_path, recovery)
    blocked = json.loads(core(tampered, "abort", apply=True, expected={2}).stdout)
    assert_true(
        blocked.get("status") == "blocked"
        and not (recovery_path.parent / "abort-result.json").exists(),
        "extra-field recovery candidate was accepted or published",
    )

    plan_tamper = make_fixture(base, "abort-plan-path-tamper", run_id="g0-abort-plan-path-tamper")
    prepare_fixture(plan_tamper)
    prepare_configured_runtime(plan_tamper)
    runtime_before_tamper = file_inventory(pathlib.Path(plan_tamper["oni"]))
    _invoke_abort_crash(
        plan_tamper,
        transaction_before_publish_hook=lambda _label, _target: (_ for _ in ()).throw(
            SystemExit("plan published before first physical step")
        ),
    )
    plan_path = fixture_paths(plan_tamper)["environment"] / "abort" / "transaction-plan.json"
    changed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    changed_plan["steps"][0]["target"] = {
        "domain": "oni_root",
        "relative": "must-not-become-an-abort-target.bin",
    }
    changed_plan["plan_hash"] = workflow.contract_hash_without(
        changed_plan,
        "plan_hash",
        "adversarial abort plan",
    )
    write_json(plan_path, changed_plan)
    plan_blocked = json.loads(core(plan_tamper, "abort", apply=True, expected={2}).stdout)
    assert_true(
        plan_blocked.get("status") == "blocked"
        and file_inventory(pathlib.Path(plan_tamper["oni"])) == runtime_before_tamper
        and not (pathlib.Path(plan_tamper["oni"]) / "must-not-become-an-abort-target.bin").exists(),
        "rehashed abort plan changed a physical path or was not rejected",
    )
    return {
        "crash_boundaries": crash_boundaries,
        "physical_boundary_count": 15,
        "pre_commit_intent_crash": "recovered_then_converged",
        "post_commit_crash": "result_converged_without_physical_replay",
        "foreign_precommit_result": "blocked_without_commit",
        "tampered_recovery_candidate": "blocked_exact_keys",
        "rehashed_plan_path_tamper": "blocked_by_reconstructed_step_layout",
    }


def test_abort_late_race_guards(base: pathlib.Path) -> dict[str, Any]:
    process = make_fixture(base, "abort-late-process", run_id="g0-abort-late-process")
    prepare_fixture(process)
    prepare_configured_runtime(process)
    before_process = file_inventory(pathlib.Path(process["oni"]))

    def start_process(_label: str, _target: pathlib.Path) -> None:
        shutil.copy2(process["game"], process["idle"])

    try:
        args = abort_namespace(process, apply=True)
        roots = workflow.resolve_roots(args)
        with workflow.exclusive_finalize_lock(roots):
            workflow.abort_locked(args, roots, transaction_before_publish_hook=start_process)
        raise VerificationFailure("late process start was accepted")
    except workflow.WorkflowError:
        pass
    finally:
        write_json(
            pathlib.Path(process["idle"]),
            {
                "schema": "oni.process-state-fixture.v1",
                "oxygen_not_included_count": 0,
                "bridge_count": 0,
            },
        )
    assert_true(
        file_inventory(pathlib.Path(process["oni"])) == before_process
        and not (fixture_paths(process)["environment"] / "abort" / "abort-result.json").exists(),
        "late process start changed ONI state or published success",
    )

    session = make_fixture(base, "abort-late-session", run_id="g0-abort-late-session")
    prepare_fixture(session)
    prepare_configured_runtime(session)
    before_loader = workflow.file_state(fixture_paths(session)["loader"])
    calls = 0

    def publish_session(_label: str, _target: pathlib.Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            write_jsonl(
                pathlib.Path(session["oni"]) / "ai-bridge" / "state.jsonl",
                [{"schema": "oni.event.v2", "event": "session_started", "session_id": "late-session"}],
            )

    try:
        args = abort_namespace(session, apply=True)
        roots = workflow.resolve_roots(args)
        with workflow.exclusive_finalize_lock(roots):
            workflow.abort_locked(args, roots, transaction_before_publish_hook=publish_session)
        raise VerificationFailure("late session evidence was accepted")
    except workflow.WorkflowError:
        pass
    assert_true(
        workflow.file_state(fixture_paths(session)["loader"]) == before_loader
        and not (fixture_paths(session)["environment"] / "abort" / "abort-result.json").exists(),
        "late session race did not rollback the prior physical step",
    )

    precommit = make_fixture(base, "abort-precommit-session", run_id="g0-abort-precommit-session")
    prepare_fixture(precommit)
    prepare_configured_runtime(precommit)
    injected_state = pathlib.Path(precommit["oni"]) / "ai-bridge" / "state.jsonl"

    def publish_precommit_session(_plan: Mapping[str, Any]) -> None:
        write_jsonl(
            injected_state,
            [{"schema": "oni.event.v2", "event": "session_started", "session_id": "precommit-race"}],
        )

    try:
        args = abort_namespace(precommit, apply=True)
        roots = workflow.resolve_roots(args)
        with workflow.exclusive_finalize_lock(roots):
            workflow.abort_locked(args, roots, transaction_before_commit_hook=publish_precommit_session)
        raise VerificationFailure("precommit session evidence was accepted")
    except workflow.WorkflowError:
        pass
    precommit_abort = fixture_paths(precommit)["environment"] / "abort"
    assert_true(
        not (precommit_abort / "abort-result.json").exists()
        and (precommit_abort / "transaction-recovery.json").exists()
        and injected_state.exists()
        and b"precommit-race" in injected_state.read_bytes(),
        "precommit session race was erased or did not retain outcome_unknown evidence",
    )

    postcommit = make_fixture(base, "abort-postcommit-process", run_id="g0-abort-postcommit-process")
    prepare_fixture(postcommit)
    prepare_configured_runtime(postcommit)

    def start_postcommit_process(_plan: Mapping[str, Any]) -> None:
        shutil.copy2(postcommit["game"], postcommit["idle"])

    try:
        args = abort_namespace(postcommit, apply=True)
        roots = workflow.resolve_roots(args)
        with workflow.exclusive_finalize_lock(roots):
            workflow.abort_locked(args, roots, transaction_after_commit_hook=start_postcommit_process)
        raise VerificationFailure("postcommit process start was accepted")
    except workflow.WorkflowError:
        pass
    finally:
        write_json(
            pathlib.Path(postcommit["idle"]),
            {
                "schema": "oni.process-state-fixture.v1",
                "oxygen_not_included_count": 0,
                "bridge_count": 0,
            },
        )
    postcommit_abort = fixture_paths(postcommit)["environment"] / "abort"
    postcommit_recovery = json.loads((postcommit_abort / "abort-recovery.json").read_text(encoding="utf-8"))
    assert_true(
        not (postcommit_abort / "abort-result.json").exists()
        and postcommit_recovery.get("state") == "abort_postcommit_guard_outcome_unknown"
        and postcommit_recovery.get("physical_commit_non_replay") is True,
        "postcommit process race published success or lost the non-replay latch",
    )

    custody: dict[str, str] = {}
    for name, path_key in (
        ("release", "release_dll"),
        ("managed_firstpass", "game_managed_dir"),
        ("source_save", "source"),
        ("startup_binding", "startup_binding_path"),
    ):
        fixture = make_fixture(base, f"abort-late-{name}", run_id=f"g0-abort-late-{name}")
        prepare_fixture(fixture)
        prepare_configured_runtime(fixture)
        path = pathlib.Path(fixture[path_key])
        if name == "managed_firstpass":
            path = path / "Assembly-CSharp-firstpass.dll"
        moved = path.with_name(path.name + ".owned")
        fired = False

        def replace_identity(_label: str, _target: pathlib.Path) -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            os.replace(path, moved)
            shutil.copy2(moved, path)

        try:
            args = abort_namespace(fixture, apply=True)
            roots = workflow.resolve_roots(args)
            with workflow.exclusive_finalize_lock(roots):
                workflow.abort_locked(args, roots, transaction_before_publish_hook=replace_identity)
            raise VerificationFailure(f"late {name} identity replacement was accepted")
        except workflow.WorkflowError:
            pass
        assert_true(
            fired and moved.exists() and path.exists()
            and not (fixture_paths(fixture)["environment"] / "abort" / "abort-result.json").exists(),
            f"late {name} custody fault was not preserved/blocked",
        )
        custody[name] = "same_bytes_new_identity_blocked"
    return {
        "late_process_start": "blocked_before_mutation",
        "late_session_evidence": "prior_step_rolled_back_no_result",
        "precommit_session_evidence": "retained_with_outcome_unknown",
        "postcommit_process_start": "nonreplay_recovery_no_result",
        "custody": custody,
    }


def test_abort_postcommit_nonreplay_and_max_path(base: pathlib.Path) -> dict[str, Any]:
    result_fault = make_fixture(base, "abort-result-fault", run_id="g0-abort-result-fault")
    result_baseline = file_inventory(pathlib.Path(result_fault["oni"]))
    prepare_fixture(result_fault)
    prepare_configured_runtime(result_fault)
    blocked = json.loads(
        core(
            result_fault,
            "abort",
            "--test-abort-result-fail-after-verify",
            apply=True,
            expected={2},
        ).stdout
    )
    assert_true(blocked.get("status") == "blocked", "abort result fault unexpectedly succeeded")
    result_root = fixture_paths(result_fault)["environment"] / "abort"
    assert_true(
        file_inventory(pathlib.Path(result_fault["oni"])) == result_baseline
        and (result_root / "abort-recovery.json").is_file()
        and not (result_root / "abort-result.json").exists(),
        "abort result failure lost the committed restore/recovery state",
    )
    plan = json.loads(core(result_fault, "abort", what_if=True).stdout)
    assert_true(
        plan.get("status") == "would_converge_committed_result"
        and plan.get("physical_transaction_replayed") is False,
        "abort recovery WhatIf did not identify non-replay convergence",
    )
    converged = json.loads(core(result_fault, "abort", apply=True).stdout)
    assert_true(converged.get("status") == "aborted", "abort result convergence failed")

    journal_fault = make_fixture(base, "abort-journal-fault", run_id="g0-abort-journal-fault")
    prepare_fixture(journal_fault)
    prepare_configured_runtime(journal_fault)
    blocked = json.loads(
        core(
            journal_fault,
            "abort",
            "--test-abort-journal-fail-after-commit",
            apply=True,
            expected={2},
        ).stdout
    )
    assert_true(blocked.get("status") == "blocked", "abort journal fault unexpectedly succeeded")
    journal_root = fixture_paths(journal_fault)["environment"] / "abort"
    published = json.loads((journal_root / "abort-result.json").read_text(encoding="utf-8"))
    replay = json.loads(core(journal_fault, "abort", apply=True).stdout)
    assert_true(replay == published, "published abort result did not converge idempotently")
    journal_events = [
        json.loads(line)["event"]
        for line in (fixture_paths(journal_fault)["environment"] / "restore-journal.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert_true(
        journal_events.count("restore_transaction_physical_committed") == 1,
        "abort postcommit recovery replayed physical state",
    )

    budget_refusal = make_fixture(base, "abort-budget-refusal", run_id="g0-abort-budget-refusal")
    prepare_fixture(budget_refusal)
    prepare_configured_runtime(budget_refusal)
    refusal_args = abort_namespace(budget_refusal, apply=False)
    refusal_roots = workflow.resolve_roots(refusal_args)
    refusal_before = file_inventory(pathlib.Path(budget_refusal["case"]))
    original_limit = workflow.MAX_LEGACY_PATH_UTF16_UNITS
    workflow.MAX_LEGACY_PATH_UTF16_UNITS = 1
    try:
        try:
            workflow.abort_locked(refusal_args, refusal_roots)
        except workflow.WorkflowError as exc:
            assert_true("path budget exceeds" in str(exc), "abort path-budget refusal used the wrong gate")
        else:
            raise VerificationFailure("over-limit abort path budget unexpectedly passed")
    finally:
        workflow.MAX_LEGACY_PATH_UTF16_UNITS = original_limit
    assert_true(
        file_inventory(pathlib.Path(budget_refusal["case"])) == refusal_before
        and not (fixture_paths(budget_refusal)["environment"] / "abort").exists(),
        "abort path budget refusal wrote preflight/staging state",
    )

    synthetic_manifest = {
        "backup": {
            "configs": [],
            "probe": {"snapshot": {"exists": False, "entries": []}},
            "old_bridge": {"snapshot": {"exists": False, "entries": []}},
            "bridge_archive_root": {"snapshot": {"exists": False, "entries": []}},
        }
    }
    synthetic_window = {
        "runtime_probe_snapshot": {"exists": False, "entries": []},
        "runtime_bridge_snapshot": {"exists": False, "entries": []},
    }
    exact_boundaries: dict[int, int] = {}
    for padding in range(1, 230):
        synthetic_roots = workflow.Roots(
            run_id="r",
            oni_root=pathlib.Path(r"C:\o"),
            evidence_root=pathlib.Path(r"C:\e") / ("p" * padding),
            boundary_report_root=pathlib.Path(r"C:\b"),
            test_root=pathlib.Path(r"C:\t"),
            live_mode=False,
        )
        try:
            measured = workflow._abort_path_budget(
                synthetic_roots,
                synthetic_manifest,
                synthetic_window,
                pathlib.Path(r"C:\o\s.sav"),
                None,
            )
        except workflow.WorkflowError:
            continue
        maximum = int(measured["max_utf16_units"])
        if maximum in {259, 260}:
            exact_boundaries[maximum] = padding
    assert_true(set(exact_boundaries) == {259, 260}, "abort path budget did not exercise 259/260")
    rejected_261 = False
    for padding in range(exact_boundaries[260] + 1, exact_boundaries[260] + 4):
        roots_261 = workflow.Roots(
            run_id="r",
            oni_root=pathlib.Path(r"C:\o"),
            evidence_root=pathlib.Path(r"C:\e") / ("p" * padding),
            boundary_report_root=pathlib.Path(r"C:\b"),
            test_root=pathlib.Path(r"C:\t"),
            live_mode=False,
        )
        try:
            workflow._abort_path_budget(
                roots_261,
                synthetic_manifest,
                synthetic_window,
                pathlib.Path(r"C:\o\s.sav"),
                None,
            )
        except workflow.WorkflowError as exc:
            if "261 UTF-16 units" in str(exc):
                rejected_261 = True
                break
    assert_true(rejected_261, "abort path budget did not reject the exact 261-unit boundary")

    long_name = "abort-maxpath-" + "x" * 16
    long_run = "g0-abort-maxpath-" + "y" * 16
    max_path = make_fixture(base, long_name, run_id=long_run)
    prepare_fixture(max_path)
    prepare_configured_runtime(max_path, add_diagnostics=True)
    install_budget = runtime_bootstrap_install_path_budget(max_path)
    max_plan = json.loads(core(max_path, "abort", what_if=True).stdout)
    max_result = json.loads(core(max_path, "abort", apply=True).stdout)
    assert_true(max_result.get("status") == "aborted", "compact abort paths failed under long TestRoot")
    artifacts = [
        path
        for path in pathlib.Path(max_path["case"]).rglob("*")
        if any(marker in path.name.casefold() for marker in (".tmp", ".claim", ".release-"))
        and not is_persistent_runtime_bootstrap_environment_lock_claim(path, max_path)
    ]
    assert_true(not artifacts, "abort left cleanup/claim artifacts under long TestRoot")
    longest = max(len(str(path)) for path in pathlib.Path(max_path["case"]).rglob("*"))
    assert_true(
        install_budget["max_utf16_units"]
        <= RUNTIME_BOOTSTRAP_LEGACY_MAX_UTF16_UNITS
        and max_plan["path_budget"]["max_utf16_units"]
        <= workflow.MAX_LEGACY_PATH_UTF16_UNITS
        and max_plan["path_budget"]["enumerated_path_count"] > 100,
        "install/abort did not enumerate a complete legacy path budget",
    )
    return {
        "result_publication_fault": "nonreplay_converged",
        "journal_ack_fault": "immutable_result_idempotent",
        "physical_commit_event_count": 1,
        "longest_test_path": longest,
        "over_limit_abort_preflight": "blocked_zero_write",
        "path_budget_boundaries": {"259": "accepted", "260": "accepted", "261": "blocked"},
        "enumerated_transient_paths": max_plan["path_budget"]["enumerated_path_count"],
        "temporary_artifact_count": 0,
    }


def sanitize_error(error: BaseException, fixture_root: pathlib.Path) -> str:
    return (
        str(error)
        .replace(str(fixture_root), "<fixture_root>")
        .replace(str(ROOT), "<workspace>")[:700]
    )


def main() -> int:
    if not POWERSHELL:
        raise SystemExit("PowerShell is required")
    tests: list[tuple[str, Callable[[pathlib.Path], dict[str, Any]]]] = [
        ("syntax_and_parser", test_syntax),
        ("live_mode_testroot_fault_preflight_refusals", test_live_mode_fault_flag_preflight_refusals),
        ("success_whatif_recursive_restore_post_compare", test_success_and_whatif),
        ("finalize_archive_absent_near_max_path", test_finalize_archive_absent_and_max_path),
        ("tamper_refusals", test_tamper_refusals),
        ("golden_baseline_cross_run_refusals", test_golden_baseline_cross_run),
        ("loader_colony_isolation_refusals", test_loader_colony_isolation_refusals),
        ("priority_pair_report_bundle_freeze_tamper_refusals", test_priority_evidence_tamper_refusals),
        ("normal_shutdown_evidence_finalizer_refusals", test_shutdown_evidence_finalizer_refusals),
        ("startup_consumption_preflight_custody_refusals", test_startup_consumption_preflight_custody_refusals),
        ("startup_consumption_committed_custody_refusals", test_startup_consumption_committed_custody_refusals),
        (
            "runtime_bootstrap_result_anchor_refusals",
            test_runtime_bootstrap_result_anchor_refusals,
        ),
        (
            "optional_old_probe_recovery_contract",
            test_optional_old_probe_recovery_contract,
        ),
        (
            "offline_runtime_bootstrap_semantic_refusals",
            test_offline_runtime_bootstrap_semantic_refusals,
        ),
        (
            "active_parent_finalize_lock_manager_custody",
            test_active_parent_finalize_lock_manager_custody,
        ),
        ("runtime_config_external_anchor_refusals", test_runtime_config_external_anchor_refusals),
        ("runtime_config_custody_concurrency_refusals", test_runtime_config_custody_concurrency_refusals),
        ("runtime_config_finalized_custody_refusals", test_runtime_config_finalized_custody_refusals),
        ("production_bridge_relative_path_contract", test_production_bridge_relative_path_contract),
        ("evicted_sources_and_confirmation_semantics", test_evicted_sources_and_confirmation_semantics),
        ("shutdown_after_verify_name_swap_refusal", test_shutdown_after_verify_name_swap),
        ("pipeline_final_full_bridge_tree_sandwich", test_pipeline_final_bridge_tree_sandwich),
        ("finalize_lock_replacement_retained", test_finalize_lock_replacement_retained),
        ("pipeline_outcome_unknown_latch_refusal", test_pipeline_outcome_unknown_refusal),
        ("pipeline_frozen_input_tamper_refusals", test_pipeline_frozen_input_tamper_refusals),
        ("cleanup_pipeline_freshness_120s_boundary", test_cleanup_pipeline_freshness_window),
        ("bridge_archive_recursive_integrity_refusals", test_bridge_archive_integrity_refusals),
        ("path_escape_root_unc_guards", test_path_and_root_guards),
        ("process_command_line_false_positive_guards", test_process_command_line_false_positive_guards),
        ("concurrent_change_refusal", test_concurrent_source_change),
        (
            "post_boundary_receipt_and_save_mtime_guards",
            test_post_boundary_receipt_and_save_mtime_guards,
        ),
        ("windows_extended_path_tree_snapshot", test_windows_extended_path_tree_snapshot),
        ("stable_descriptor_evidence_reads", test_stable_descriptor_evidence_reads),
        ("transaction_rollback_ownership_refusals", test_transaction_rollback_ownership_refusals),
        ("transaction_journal_fault_state_machine", test_transaction_journal_fault_state_machine),
        (
            "finalize_transaction_epoch_reducer_and_latch",
            test_finalize_transaction_epoch_reducer_and_latch,
        ),
        ("atomic_publication_prepare_stage_ownership", test_atomic_publication_and_prepare_stage_ownership),
        ("copy_staging_descriptor_ownership", test_copy_staging_descriptor_ownership),
        ("finalize_result_nonreplay_recovery", test_finalize_result_nonreplay_recovery),
        (
            "finalize_fresh_revalidation_after_latch_truncation",
            test_finalize_fresh_revalidation_after_latch_truncation,
        ),
        (
            "finalize_postcommit_custody_result_authentication",
            test_finalize_postcommit_custody_and_result_authentication,
        ),
        ("process_guard_partial_failure_rollback_journal", test_guards_and_partial_rollback),
        ("abort_success_partial_diagnostics_nonreplay", test_abort_success_partial_and_replay),
        ("abort_session_action_zero_write_refusals", test_abort_session_action_refusals),
        ("abort_anchor_identity_process_lock_refusals", test_abort_anchor_identity_and_lock_refusals),
        ("abort_all_step_rollback_external_replacement", test_abort_all_step_rollback_and_external_replacement),
        ("abort_durable_crash_recovery_result_authentication", test_abort_durable_crash_recovery_and_result_authentication),
        ("abort_late_race_guards", test_abort_late_race_guards),
        ("abort_postcommit_recovery_cleanup_maxpath", test_abort_postcommit_nonreplay_and_max_path),
    ]
    results: list[dict[str, Any]] = []
    # Keep the shared TestRoot prefix compact.  Individual MAX_PATH scenarios
    # construct their own measured 259/260/261-unit boundaries; a long global
    # prefix would make unrelated custody fixtures accidentally exercise that
    # boundary first.
    with tempfile.TemporaryDirectory(prefix="oe-") as temporary:
        fixture_root = pathlib.Path(temporary).resolve()
        for name, test in tests:
            try:
                evidence = test(fixture_root)
                results.append({"name": name, "status": "passed", "evidence": evidence})
            except BaseException as exc:
                results.append(
                    {
                        "name": name,
                        "status": "failed",
                        "error": sanitize_error(exc, fixture_root),
                    }
                )
    passed = sum(item["status"] == "passed" for item in results)
    payload = {
        "schema": "oni.live-validation-environment-verification.v1",
        "status": "passed" if passed == len(results) else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation_level": "offline_manifest_transaction_fixture",
        "scope": "temporary_TestRoot_only",
        "live_oni_accessed": False,
        "game_or_bridge_launched": False,
        "test_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "assertion_count": sum(
            len(item.get("evidence", {}))
            for item in results
            if item.get("status") == "passed" and isinstance(item.get("evidence"), dict)
        ),
        "scripts": {
            CORE.name: sha256(CORE),
            "configure_live_validation_runtime.py": sha256(
                SCRIPTS / "configure_live_validation_runtime.py"
            ),
            "manage_live_runtime_bootstrap.py": sha256(
                SCRIPTS / "manage_live_runtime_bootstrap.py"
            ),
            "create_shutdown_evidence.py": sha256(SCRIPTS / "create_shutdown_evidence.py"),
            "verify_shutdown_evidence.py": sha256(SCRIPTS / "verify_shutdown_evidence.py"),
            "create_startup_consumption_evidence.py": sha256(
                SCRIPTS / "create_startup_consumption_evidence.py"
            ),
            "verify_live_validation_runtime_config.py": sha256(
                SCRIPTS / "verify_live_validation_runtime_config.py"
            ),
            pathlib.Path(__file__).name: sha256(pathlib.Path(__file__).resolve()),
        },
        "tests": results,
    }
    report_contract = dict(payload)
    del report_contract["generated_at"]
    payload["report_hash"] = workflow.sha256_bytes(workflow.canonical_bytes(report_contract))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = REPORT.with_name(f".{REPORT.name}.{os.getpid()}.tmp")
    temporary_report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_report, REPORT)
    print(json.dumps({"status": payload["status"], "passed": passed, "total": len(results)}))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
