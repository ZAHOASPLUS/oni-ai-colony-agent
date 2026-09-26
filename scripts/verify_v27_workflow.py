#!/usr/bin/env python3
"""Verify exact-tip policy rotation and guarded runtime preparation contracts."""

from __future__ import annotations

import json
import hashlib
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import re


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY_SCRIPT = PROJECT_ROOT / "scripts" / "set-v27-validation-policy.ps1"
PREPARE_SCRIPT = PROJECT_ROOT / "scripts" / "prepare-v27-runtime.ps1"
BOOTSTRAP_MANAGER = PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py"
RUN_BRIDGE_SCRIPT = PROJECT_ROOT / "scripts" / "run-bridge.ps1"
STARTUP_BINDING_SOURCE = PROJECT_ROOT / "mod" / "Source" / "Probe" / "StartupBinding.cs"
STARTUP_EVIDENCE_CREATOR = PROJECT_ROOT / "scripts" / "create_startup_consumption_evidence.py"
STARTUP_BINDING_VERIFIER = PROJECT_ROOT / "scripts" / "verify_probe_startup_binding.py"
STARTUP_EVIDENCE_VERIFIER = PROJECT_ROOT / "scripts" / "verify_startup_consumption_evidence.py"
SHUTDOWN_EVIDENCE_VERIFIER = PROJECT_ROOT / "scripts" / "verify_shutdown_evidence.py"
REPORT_PATH = PROJECT_ROOT / "reports" / "v27-workflow-verification.json"
SAVE_HASH = "sha256:" + "a" * 64
RUN_BRIDGE_HELP_MARKER = "oni-bridge-help-side-effect-free"
RELEASE_ARTIFACT_NAMES = (
    "OniAiProbeMod.dll",
    "OniAiProbeMod.pdb",
    "mod.yaml",
    "mod_info.yaml",
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STARTUP_RUNTIME_RESULT_FIELDS = {
    "path_id_lower": {
        "binding_field": "runtime_bootstrap_result_path_id_lower",
        "record_fields": ("path_id_lower",),
    },
    "raw_sha256": {
        "binding_field": "runtime_bootstrap_result_sha256",
        "record_fields": ("external_expected_sha256", "raw_sha256"),
    },
    "result_hash": {
        "binding_field": "runtime_bootstrap_result_hash",
        "record_fields": ("result_hash",),
    },
    "transaction_id": {
        "binding_field": "runtime_bootstrap_transaction_id",
        "record_fields": ("transaction_id",),
    },
    "probe_tree_content_hash": {
        "binding_field": "installed_probe_tree_content_hash",
        "record_fields": ("probe_tree_content_hash",),
    },
}


def write_json(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(POLICY_SCRIPT), *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )


def release_artifacts_match(
    build_receipt: dict[str, object], what_if: dict[str, object]
) -> bool:
    """Typed exact comparison required before accepting the stage SHA/id."""

    left = build_receipt.get("artifacts")
    right = what_if.get("release_artifacts")
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    expected_names = set(RELEASE_ARTIFACT_NAMES)
    if set(left) != expected_names or set(right) != expected_names:
        return False
    for name in RELEASE_ARTIFACT_NAMES:
        for record in (left[name], right[name]):
            if (
                not isinstance(record, dict)
                or set(record) != {"length", "sha256"}
                or type(record.get("length")) is not int
                or record["length"] < 0
                or not isinstance(record.get("sha256"), str)
                or SHA256_RE.fullmatch(record["sha256"]) is None
            ):
                return False
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def verify_release_build_receipt(root: pathlib.Path) -> dict[str, object]:
    """Exercise the build wrapper with a TestRoot-only deterministic fixture."""

    root.mkdir(parents=True, exist_ok=True)
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not powershell:
        return {"passed": False, "reason": "powershell_unavailable"}
    fixture_script = root / "fake-build.ps1"
    release_dir = root / "release"
    release_dll = release_dir / "OniAiProbeMod.dll"
    managed_dir = root / "managed"
    binding_path = root / "build-parameter-binding.json"
    fixture_script.write_text(
        """param([string]$Configuration='Release',[string]$ONIManagedDir='')
$ErrorActionPreference='Stop'
$release=Join-Path $PSScriptRoot 'release'
New-Item -ItemType Directory -Force -Path $release | Out-Null
[ordered]@{
 Configuration=$Configuration
 ONIManagedDir=$ONIManagedDir
} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'build-parameter-binding.json') -Encoding UTF8
$payloads=[ordered]@{
 'OniAiProbeMod.dll'='fixture-dll'
 'OniAiProbeMod.pdb'='fixture-pdb'
 'mod.yaml'='fixture-mod-yaml'
 'mod_info.yaml'='fixture-mod-info'
}
foreach($entry in $payloads.GetEnumerator()){
 [IO.File]::WriteAllBytes((Join-Path $release $entry.Key),[Text.Encoding]::UTF8.GetBytes($entry.Value))
}
Write-Host 'captured fake build log'
$global:LASTEXITCODE=0
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PREPARE_SCRIPT),
            "-BuildRelease",
            "-ReleaseDll",
            str(release_dll),
            "-BuildConfiguration",
            "Release",
            "-ONIManagedDir",
            str(managed_dir),
            "-TestRoot",
            str(root),
            "-TestBuildScriptFixture",
            str(fixture_script),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    try:
        receipt = json.loads(completed.stdout)
    except json.JSONDecodeError:
        receipt = None
    try:
        parameter_binding = json.loads(binding_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        parameter_binding = None
    expected_artifacts = {
        name: {
            "length": (release_dir / name).stat().st_size,
            "sha256": "sha256:"
            + hashlib.sha256((release_dir / name).read_bytes()).hexdigest(),
        }
        for name in RELEASE_ARTIFACT_NAMES
        if (release_dir / name).is_file()
    }
    passed = (
        completed.returncode == 0
        and completed.stderr == ""
        and isinstance(receipt, dict)
        and receipt.get("schema") == "oni.live-runtime-bootstrap-release-build.v1"
        and receipt.get("status") == "release_built_for_external_freeze"
        and receipt.get("artifacts") == expected_artifacts
        and receipt.get("expected_release_dll_sha256")
        == expected_artifacts.get("OniAiProbeMod.dll", {}).get("sha256")
        and receipt.get("manager_invoked") is False
        and receipt.get("runtime_write_count") == 0
        and receipt.get("game_launched") is False
        and parameter_binding
        == {"Configuration": "Release", "ONIManagedDir": str(managed_dir)}
    )
    return {
        "passed": passed,
        "returncode": completed.returncode,
        "stdout_is_single_json_document": isinstance(receipt, dict),
        "stderr_empty": completed.stderr == "",
        "artifact_count": len(expected_artifacts),
        "named_parameter_binding_exact": parameter_binding
        == {"Configuration": "Release", "ONIManagedDir": str(managed_dir)},
        "manager_invoked": receipt.get("manager_invoked")
        if isinstance(receipt, dict)
        else None,
    }


def install_fake_python_launchers(fake_bin: pathlib.Path, launch_marker: pathlib.Path) -> None:
    """Intercept a regressed launcher body without starting project Python."""

    fake_bin.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        command = f'@echo off\r\n> "{launch_marker}" echo launched\r\nexit /b 93\r\n'
        for name in ("py.cmd", "python.cmd"):
            (fake_bin / name).write_text(command, encoding="utf-8")
        return
    command = f'#!/bin/sh\nprintf launched > "{launch_marker}"\nexit 93\n'
    for name in ("py", "python"):
        path = fake_bin / name
        path.write_text(command, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def verify_run_bridge_help(root: pathlib.Path) -> dict[str, object]:
    """Prove help/unknown arguments fail before the Bridge body can execute."""

    powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return {
            "passed": False,
            "reason": "powershell_unavailable",
            "bridge_dir_created": False,
        }
    home = root / "home"
    temporary = root / "temp"
    fake_bin = root / "fake-bin"
    sentinel = home / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"
    launch_marker = root / "launcher-body-entered.txt"
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    install_fake_python_launchers(fake_bin, launch_marker)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "ONI_AI_BRIDGE_DIR": str(sentinel),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
        }
    )

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(RUN_BRIDGE_SCRIPT),
                *arguments,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
            env=environment,
        )

    help_result = run("-BridgeDir", str(sentinel), "-Once", "-?")
    help_output = help_result.stdout + help_result.stderr
    help_marker_rendered = RUN_BRIDGE_HELP_MARKER in help_output.casefold()
    help_body_entered = launch_marker.exists()
    if help_body_entered:
        launch_marker.unlink()
    invalid_result = run("-BridgeDir", str(sentinel), "-Once", "-DefinitelyInvalid")
    invalid_body_entered = launch_marker.exists()
    if invalid_body_entered:
        launch_marker.unlink()
    partial_expectation_result = run(
        "-BridgeDir",
        str(sentinel),
        "-Once",
        "-ExpectedValidationRunId",
        "partial-startup-contract",
    )
    partial_expectation_body_entered = launch_marker.exists()
    if partial_expectation_body_entered:
        launch_marker.unlink()
    pid_only_result = run(
        "-BridgeDir",
        str(sentinel),
        "-Once",
        "-ExpectedValidationProcessId",
        "4242",
    )
    pid_only_body_entered = launch_marker.exists()
    if pid_only_body_entered:
        launch_marker.unlink()
    invalid_pid_result = run(
        "-BridgeDir",
        str(sentinel),
        "-Once",
        "-ExpectedValidationProcessId",
        "-1",
    )
    invalid_pid_body_entered = launch_marker.exists()
    if invalid_pid_body_entered:
        launch_marker.unlink()
    zero_pid_result = run(
        "-BridgeDir",
        str(sentinel),
        "-Once",
        "-ExpectedValidationProcessId",
        "0",
    )
    zero_pid_body_entered = launch_marker.exists()
    if zero_pid_body_entered:
        launch_marker.unlink()
    bridge_dir_created = sentinel.exists()
    return {
        "passed": (
            help_result.returncode == 0
            and help_marker_rendered
            and not help_body_entered
            and invalid_result.returncode != 0
            and not invalid_body_entered
            and partial_expectation_result.returncode != 0
            and not partial_expectation_body_entered
            and pid_only_result.returncode != 0
            and not pid_only_body_entered
            and invalid_pid_result.returncode != 0
            and not invalid_pid_body_entered
            and zero_pid_result.returncode != 0
            and not zero_pid_body_entered
            and not bridge_dir_created
        ),
        "help_returncode": help_result.returncode,
        "help_marker_rendered": help_marker_rendered,
        "help_body_entered": help_body_entered,
        "invalid_parameter_returncode": invalid_result.returncode,
        "invalid_parameter_body_entered": invalid_body_entered,
        "partial_expectation_returncode": partial_expectation_result.returncode,
        "partial_expectation_body_entered": partial_expectation_body_entered,
        "pid_only_returncode": pid_only_result.returncode,
        "pid_only_body_entered": pid_only_body_entered,
        "invalid_pid_returncode": invalid_pid_result.returncode,
        "invalid_pid_body_entered": invalid_pid_body_entered,
        "zero_pid_returncode": zero_pid_result.returncode,
        "zero_pid_body_entered": zero_pid_body_entered,
        "bridge_dir_created": bridge_dir_created,
        "python_launchers": "TestRoot interceptors",
        "home_and_temp": "TestRoot",
    }


def verify_startup_v2_runtime_result_visibility() -> dict[str, object]:
    """Expose the five bootstrap-result bindings in the umbrella report."""

    source_paths = {
        "probe_runtime_consumer": STARTUP_BINDING_SOURCE,
        "startup_evidence_consumer": STARTUP_EVIDENCE_CREATOR,
        "probe_contract_verifier": STARTUP_BINDING_VERIFIER,
        "startup_evidence_verifier": STARTUP_EVIDENCE_VERIFIER,
        "shutdown_evidence_verifier": SHUTDOWN_EVIDENCE_VERIFIER,
    }
    sources = {
        name: path.read_text(encoding="utf-8") for name, path in source_paths.items()
    }
    checks: list[bool] = []
    fields: dict[str, object] = {}
    for semantic_name, contract in STARTUP_RUNTIME_RESULT_FIELDS.items():
        binding_field = str(contract["binding_field"])
        record_fields = tuple(str(value) for value in contract["record_fields"])
        source_visibility = {
            source_name: binding_field in source
            and (
                source_name
                in {"probe_runtime_consumer", "probe_contract_verifier"}
                or (
                    source_name == "shutdown_evidence_verifier"
                    and "make_runtime_bootstrap_result" in source
                )
                or all(record_field in source for record_field in record_fields)
            )
            for source_name, source in sources.items()
        }
        checks.extend(source_visibility.values())
        fields[semantic_name] = {
            "binding_field": binding_field,
            "runtime_result_record_fields": list(record_fields),
            "source_visibility": source_visibility,
            "visible_end_to_end": all(source_visibility.values()),
        }

    schema_checks = {
        "probe_binding_v2": 'BindingSchema = "oni.runtime-startup-binding.v2"'
        in sources["probe_runtime_consumer"],
        "creator_binding_v2": 'BINDING_SCHEMA = "oni.runtime-startup-binding.v2"'
        in sources["startup_evidence_consumer"],
        "creator_intent_v2": 'INTENT_SCHEMA = "oni.live-validation-startup-intent.v2"'
        in sources["startup_evidence_consumer"],
        "creator_evidence_v3": 'EVIDENCE_SCHEMA = "oni.startup-consumption-evidence.v3"'
        in sources["startup_evidence_consumer"],
        "legacy_binding_v1_refused_by_probe_verifier": (
            'legacy["schema"] = "oni.runtime-startup-binding.v1"'
            in sources["probe_contract_verifier"]
        ),
        "legacy_binding_v1_refused_by_startup_verifier": (
            'legacy_binding["schema"] = "oni.runtime-startup-binding.v1"'
            in sources["startup_evidence_verifier"]
        ),
        "legacy_intent_v1_refused": (
            'legacy_intent["startup_intent"]["schema"] = '
            '"oni.live-validation-startup-intent.v1"'
            in sources["startup_evidence_verifier"]
        ),
        "legacy_runtime_config_v4_refused": (
            'legacy_runtime["schema"] = "oni.live-validation-runtime-config.v4"'
            in sources["startup_evidence_verifier"]
        ),
    }
    checks.extend(schema_checks.values())

    values = {
        "path_id_lower": "sha256:" + "11" * 32,
        "raw_sha256": "sha256:" + "22" * 32,
        "result_hash": "sha256:" + "33" * 32,
        "transaction_id": "rtb-" + "44" * 12,
        "probe_tree_content_hash": "sha256:" + "55" * 32,
    }
    binding = {
        "runtime_bootstrap_result_path_id_lower": values["path_id_lower"],
        "runtime_bootstrap_result_sha256": values["raw_sha256"],
        "runtime_bootstrap_result_hash": values["result_hash"],
        "runtime_bootstrap_transaction_id": values["transaction_id"],
        "installed_probe_tree_content_hash": values["probe_tree_content_hash"],
    }
    record = {
        "path_id_lower": values["path_id_lower"],
        "external_expected_sha256": values["raw_sha256"],
        "raw_sha256": values["raw_sha256"],
        "result_hash": values["result_hash"],
        "transaction_id": values["transaction_id"],
        "probe_tree_content_hash": values["probe_tree_content_hash"],
    }

    def fixture_matches(candidate: dict[str, object]) -> bool:
        return (
            candidate.get("path_id_lower")
            == binding["runtime_bootstrap_result_path_id_lower"]
            and candidate.get("external_expected_sha256")
            == binding["runtime_bootstrap_result_sha256"]
            and candidate.get("raw_sha256")
            == binding["runtime_bootstrap_result_sha256"]
            and candidate.get("result_hash")
            == binding["runtime_bootstrap_result_hash"]
            and candidate.get("transaction_id")
            == binding["runtime_bootstrap_transaction_id"]
            and candidate.get("probe_tree_content_hash")
            == binding["installed_probe_tree_content_hash"]
        )

    fixture_checks = {
        "path_id_lower_cross_binding": record["path_id_lower"]
        == binding["runtime_bootstrap_result_path_id_lower"],
        "external_raw_sha_cross_binding": record["external_expected_sha256"]
        == binding["runtime_bootstrap_result_sha256"],
        "stable_raw_sha_cross_binding": record["raw_sha256"]
        == binding["runtime_bootstrap_result_sha256"],
        "result_hash_cross_binding": record["result_hash"]
        == binding["runtime_bootstrap_result_hash"],
        "transaction_id_cross_binding": record["transaction_id"]
        == binding["runtime_bootstrap_transaction_id"],
        "probe_tree_hash_cross_binding": record["probe_tree_content_hash"]
        == binding["installed_probe_tree_content_hash"],
    }
    tampered_record = dict(record)
    tampered_record["raw_sha256"] = "sha256:" + "66" * 32
    fixture_checks["resigned_or_tampered_raw_sha_refused"] = not fixture_matches(
        tampered_record
    )
    checks.extend(fixture_checks.values())
    return {
        "passed": all(checks),
        "assertion_count": len(checks),
        "binding_schema": "oni.runtime-startup-binding.v2",
        "startup_intent_schema": "oni.live-validation-startup-intent.v2",
        "startup_evidence_schema": "oni.startup-consumption-evidence.v3",
        "fields": fields,
        "schema_and_legacy_refusal_checks": schema_checks,
        "exact_fixture_checks": fixture_checks,
        "specialist_verifiers": {
            name: path.relative_to(PROJECT_ROOT).as_posix()
            for name, path in source_paths.items()
            if name.endswith("verifier")
        },
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="oni-v27-workflow-") as temporary:
        root = pathlib.Path(temporary)
        bridge = root / "bridge"
        for directory in (
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
            (bridge / "commands" / directory).mkdir(parents=True, exist_ok=True)
        state = {
            "session_id": "session-fixture",
            "save_id_hash": SAVE_HASH,
            "save_dirty": False,
            "world_id": 0,
            "bridge": {"stale": False},
        }
        manifest = {
            "schema": "oni.test-save-lineage.v1",
            "status": "active",
            "latest": {
                "session_id": "session-fixture",
                "save_id_hash": SAVE_HASH,
                "world_id": 0,
            },
        }
        state_path = bridge / "latest_state.json"
        manifest_path = root / "manifest.json"
        write_json(state_path, state)
        write_json(manifest_path, manifest)

        enabled = invoke(
            "-Manifest",
            str(manifest_path),
            "-BridgeDir",
            str(bridge),
            "-EnableActions",
            "-AllowedActions",
            "noop",
        )
        enabled_policy = json.loads((bridge / "action-policy.json").read_text(encoding="utf-8-sig"))
        exact_tip = (
            enabled.returncode == 0
            and enabled_policy.get("actions_enabled") is True
            and enabled_policy.get("allowed_save_hashes") == [SAVE_HASH]
            and enabled_policy.get("allowed_actions") == ["noop"]
        )

        active_queue_blocks = {}
        for queue_name in ("pending", "approving", "approved", "executing", "confirming"):
            blocked_path = bridge / "commands" / queue_name / "blocked.json"
            write_json(blocked_path, {"id": "blocked", "status": queue_name})
            blocked = invoke(
                "-Manifest",
                str(manifest_path),
                "-BridgeDir",
                str(bridge),
                "-EnableActions",
                "-AllowedActions",
                "noop",
            )
            active_queue_blocks[queue_name] = blocked.returncode != 0
            blocked_path.unlink()

        disabled = invoke("-Manifest", str(manifest_path), "-BridgeDir", str(bridge))
        disabled_policy = json.loads((bridge / "action-policy.json").read_text(encoding="utf-8-sig"))
        fail_closed = (
            disabled.returncode == 0
            and disabled_policy.get("actions_enabled") is False
            and disabled_policy.get("allowed_save_hashes") == []
        )

        run_bridge_help = verify_run_bridge_help(root)
        release_build_receipt = verify_release_build_receipt(root / "build-receipt")
        startup_v2_runtime_result = verify_startup_v2_runtime_result_visibility()

    prepare_source = PREPARE_SCRIPT.read_text(encoding="utf-8")
    manager_source = BOOTSTRAP_MANAGER.read_text(encoding="utf-8")
    build_call_index = prepare_source.find("& $BuildScript @BuildArguments 3>&1")
    build_exit_index = prepare_source.find("exit 0", build_call_index)
    runtime_mode_index = prepare_source.find("$ModeCount =", build_exit_index)
    manager_call_index = prepare_source.find("& $PythonLauncher.Source -3 @Arguments")
    preparation_guards = (
        all(
            fragment in prepare_source
            for fragment in (
                "[ValidateSet('Install', 'Abort')]",
                "[switch]$Stage",
                "[switch]$RecoverStage",
                "[switch]$Apply",
                "[switch]$WhatIf",
                "BuildRelease is a standalone workspace-only phase.",
                "release_built_for_external_freeze",
                "expected_release_dll_sha256",
                "manager_invoked = $false",
                "$BuildArguments = @{",
                "Configuration = $BuildConfiguration",
                "$BuildArguments['ONIManagedDir'] = $ONIManagedDir",
                "Every runtime phase requires the caller-frozen StageNonce.",
                "Runtime phase requires $($RequiredArgument.Key).",
                "--expected-stage-sha256",
                "--expected-install-result-sha256",
                "--pre-boundary",
                "--clone-manifest",
                "--source-save",
                "--loader-save",
                "--loader-stem",
            )
        )
        and build_call_index >= 0
        and build_exit_index > build_call_index
        and runtime_mode_index > build_exit_index
        and manager_call_index > runtime_mode_index
        and "Live Install WhatIf requires -BuildRelease" not in prepare_source
        and "dotnet" not in manager_source.casefold()
        and "subprocess.popen" not in manager_source.casefold()
        and "start-process" not in prepare_source.casefold()
    )
    artifact_fixture = {
        name: {"length": index + 1, "sha256": "sha256:" + f"{index + 1:064x}"}
        for index, name in enumerate(RELEASE_ARTIFACT_NAMES)
    }
    build_receipt_fixture: dict[str, object] = {
        "artifacts": json.loads(json.dumps(artifact_fixture))
    }
    what_if_fixture: dict[str, object] = {
        "release_artifacts": json.loads(json.dumps(artifact_fixture))
    }
    tampered_what_if = json.loads(json.dumps(what_if_fixture))
    tampered_what_if["release_artifacts"]["mod.yaml"]["length"] += 1
    release_receipt_binding = (
        release_artifacts_match(build_receipt_fixture, what_if_fixture)
        and not release_artifacts_match(build_receipt_fixture, tampered_what_if)
        and '"release_artifacts": {' in manager_source
        and "expected_release_dll_sha256 = $Artifacts['OniAiProbeMod.dll'].sha256"
        in prepare_source
    )
    report = {
        "schema": "oni.v27-workflow-verification.v1",
        "scenario_count": 8,
        "assertion_count": 55 + int(startup_v2_runtime_result["assertion_count"]),
        "status": "passed"
        if exact_tip
        and all(active_queue_blocks.values())
        and fail_closed
        and preparation_guards
        and release_receipt_binding
        and release_build_receipt.get("passed") is True
        and run_bridge_help.get("passed") is True
        and startup_v2_runtime_result.get("passed") is True
        else "failed",
        "exact_current_tip_policy": exact_tip,
        "active_queues_block_policy_change": active_queue_blocks,
        "disable_revokes_all_save_hashes": fail_closed,
        "runtime_preparation_guards_present": preparation_guards,
        "release_build_receipt_matches_what_if_artifacts": release_receipt_binding,
        "release_build_stdout_contract": release_build_receipt,
        "run_bridge_help_no_launch": run_bridge_help,
        "startup_v2_runtime_bootstrap_result_chain": startup_v2_runtime_result,
    }
    write_json(REPORT_PATH, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Report: {REPORT_PATH}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
