#!/usr/bin/env python3
"""Verify the validation-only save/reload control contract."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import tempfile
from typing import Any

from manage_validation_checkpoint import (
    CheckpointError,
    build_control,
    build_policy,
    normalized_path,
    path_id,
    validate_environment,
    write_disabled_policy,
    write_json_atomic,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def file_hash(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_policy_and_request() -> dict:
    with tempfile.TemporaryDirectory(prefix="oni-validation-control-") as temporary:
        root = pathlib.Path(temporary)
        save_dir = root / "colony"
        save_dir.mkdir()
        save_path = save_dir / "active.sav"
        save_path.write_bytes(b"save-bytes")
        state = {
            "session_id": "session-test",
            "snapshot_id": "session-test:7",
            "save_id_hash": file_hash(save_path),
        }
        policy = build_policy(
            enabled=True,
            test_run_id="test-run",
            operation="save_checkpoint",
            save_hash=state["save_id_hash"],
            save_path_id=path_id(save_path),
            target_save_path_id=path_id(save_dir / "new.sav"),
        )
        request = build_control(
            control_id="ctl-test",
            operation="save_checkpoint",
            test_run_id="test-run",
            state=state,
            save_path_id=path_id(save_path),
            expected_mod_version="0.7.0",
            target_save_path=save_dir / "new.sav",
            ttl_seconds=60,
        )
        require(policy["controls_enabled"] is True, "enabled policy was not enabled")
        require(policy["allowed_operations"] == ["save_checkpoint"], "operation whitelist broadened")
        require(policy["allowed_save_hashes"] == [state["save_id_hash"]], "save whitelist broadened")
        require(request["requires_approval"] is True, "control lost explicit approval marker")
        require(request["source_snapshot_id"] == "session-test:7", "snapshot binding missing")
        require(request["args"]["target_save_path_id"] == path_id(save_dir / "new.sav"), "target binding missing")

        write_disabled_policy(root)
        disabled = json.loads((root / "validation-control-policy.json").read_text(encoding="utf-8"))
        require(disabled["controls_enabled"] is False, "disabled policy is enabled")
        require(disabled["allowed_save_hashes"] == [], "disabled policy retained save hashes")
        return {"policy_exact": True, "approval_bound": True, "default_disabled": True}


def verify_environment_gate() -> dict:
    with tempfile.TemporaryDirectory(prefix="oni-validation-environment-") as temporary:
        root = pathlib.Path(temporary)
        save_path = root / "active.sav"
        save_path.write_bytes(b"save")
        write_json_atomic(
            root / "action-policy.json",
            {
                "schema": "oni.action-policy.v1",
                "actions_enabled": False,
                "suggestions_enabled": False,
                "allowed_save_hashes": [],
                "allowed_actions": ["noop"],
            },
        )
        state = {
            "mod_version": "0.7.0",
            "save_dirty": False,
            "save_id_hash": file_hash(save_path),
        }
        validate_environment(root, state, save_path, "0.7.0")
        blocked_queues = {}
        for queue_name in ("pending", "approving", "approved", "executing", "confirming"):
            queue = root / "commands" / queue_name
            queue.mkdir(parents=True, exist_ok=True)
            marker = queue / "cmd.json"
            marker.write_text("{}\n", encoding="utf-8")
            rejected = False
            try:
                validate_environment(root, state, save_path, "0.7.0")
            except CheckpointError:
                rejected = True
            marker.unlink()
            blocked_queues[queue_name] = rejected
        require(all(blocked_queues.values()), "checkpoint environment accepted an active action queue")
        return {
            "disk_hash_bound": True,
            "action_queue_quiescence": True,
            "blocked_queues": blocked_queues,
        }


def verify_manager_confirmation_contract() -> dict:
    source = (ROOT / "scripts" / "manage_validation_checkpoint.py").read_text(encoding="utf-8")
    required = (
        'ACTIVE_ACTION_QUEUES = ("pending", "approving", "approved", "executing", "confirming")',
        'state_at >= terminal_at',
        'and (require_new_session or seq > minimum_seq)',
        '"classification": "control_plane_verified"',
        '"game_action_confirmation": False',
        'default="0.7.0"',
    )
    require(all(fragment in source for fragment in required), "manager lost fail-closed post-state confirmation")
    return {
        "later_snapshot_required": True,
        "game_action_scope_excluded": True,
        "expected_mod_version": "0.7.0",
    }


def verify_mod_contract() -> dict:
    source = (ROOT / "mod" / "Source" / "Probe" / "ValidationCheckpointController.cs").read_text(encoding="utf-8")
    session = (ROOT / "mod" / "Source" / "Probe" / "ProbeSession.cs").read_text(encoding="utf-8")
    runner = (ROOT / "mod" / "Source" / "Probe" / "ProbeRunner.cs").read_text(encoding="utf-8")
    required = (
        "SaveLoader.Instance.Save(",
        "LoadingOverlay.Load(delegate",
        "LoadScreen.DoLoad(activePath);",
        'ReloadIntentFileName = "reload-intent.json"',
        "ValidateActionQueueIsQuiescent()",
        '"allowed_target_save_path_ids"',
        "audit.Generation > beforeGeneration",
        "sessionChanged && saveMatches && pathMatches && stateReadable",
    )
    require(all(marker in source for marker in required), "mod validation control contract is incomplete")
    require("LoadScreen.ForceStopGame();" not in source, "controller duplicated the load screen teardown flow")
    require("SaveLoader.Instance.Load(" not in source, "controller regressed to low-level SaveLoader.Load")
    require("lastSaveWriteCompletion" in session and "saveWriteGeneration" in session, "save audit generation is missing")
    require(
        "ValidationCheckpointController.BeginSession();" in runner
        and "ValidationCheckpointController.Update(Time.unscaledTime);" in runner,
        "validation controller is not wired into the runner",
    )
    return {
        "native_save": True,
        "native_reload": True,
        "official_load_screen_flow": True,
        "durable_reload_intent": True,
    }


def verify_reload_recovery_contract() -> dict:
    source = (ROOT / "mod" / "Source" / "Probe" / "ValidationCheckpointController.cs").read_text(encoding="utf-8")
    required = (
        "FinalizeReloadRecoveryFailure(intentPath, intent, ex)",
        "RecoveryInflightPaths(intent, dirs)",
        'string.Equals(ReadString(control, "operation"), "reload_checkpoint"',
        'string.Equals(ReadString(control, "status"), "inflight"',
        '"replayed"] = false',
        'StableRecoveryId(Path.GetFileName(inflightPath))',
        'QuarantineRecoverySource(intentPath, dirs.ReloadRecoveryQuarantine',
        'QuarantineRecoverySource(\n                    inflightPath',
    )
    require(all(marker in source for marker in required), "reload recovery hardening contract is incomplete")

    selector = re.search(
        r"private static IEnumerable<string> RecoveryInflightPaths.*?\n        }\n",
        source,
        flags=re.DOTALL,
    )
    require(selector is not None, "reload recovery selector is missing")
    selector_source = selector.group(0)
    require('Path.GetExtension(inflightFile), ".json"' in selector_source, "intent binding accepts non-JSON paths")
    require(
        '!string.Equals(ReadString(boundControl, "operation"), "reload_checkpoint"' in selector_source,
        "corrupt intent can consume a bound non-reload control",
    )
    require('return new[] { boundPath };' in selector_source, "bound corrupt inflight is not isolated")
    require('matches.Add(candidate);' in selector_source, "restart recovery does not identify reload inflight")
    corrupt_catch = re.search(r"catch \(JsonException\).*?matches.Add\(candidate\);", selector_source, flags=re.DOTALL)
    require(corrupt_catch is not None, "ambiguous corrupt inflight is not quarantined")
    require("catch (IOException)" in selector_source, "unreadable inflight is not quarantined")
    require("catch (UnauthorizedAccessException)" in selector_source, "access-denied inflight is not quarantined")

    recovery = re.search(
        r"private static void FinalizeReloadRecoveryFailure.*?\n        }\n\n        private static IEnumerable<string>",
        source,
        flags=re.DOTALL,
    )
    require(recovery is not None, "reload recovery finalizer is missing")
    require(
        'Directory.GetFiles(dirs.Inflight, "*.json")' not in recovery.group(0),
        "corrupt intent still terminalizes every inflight control",
    )
    require(
        'DateTimeOffset.Now.ToUnixTimeMilliseconds()' not in source[source.index("private static JObject FinalizeOrQuarantineRecoveredControl"):source.index("private static string QuarantineRecoverySource")],
        "corrupt inflight fallback identity is time-dependent",
    )
    require(
        'string reason = "Reload intent recovery failed; the control was not replayed.";' in recovery.group(0),
        "recovery terminal message depends on platform-specific parse errors",
    )
    return {
        "corrupt_intent_terminal": True,
        "corrupt_inflight_quarantined": True,
        "restart_recovery_no_replay": True,
        "unrelated_inflight_preserved": True,
        "stable_fallback_identity": True,
    }


def _offline_recover_reload(root: pathlib.Path) -> list[dict[str, Any]]:
    """Exercise the controller's documented recovery state transition on fixtures."""
    inflight = root / "inflight"
    failed = root / "failed"
    quarantine = root / "quarantine"
    recovery = quarantine / "reload-recovery"
    for directory in (inflight, failed, quarantine, recovery):
        directory.mkdir(parents=True, exist_ok=True)

    intent_path = root / "reload-intent.json"
    if not intent_path.exists():
        return []
    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        intent = None

    bound = intent.get("inflight_file") if isinstance(intent, dict) else None
    candidates: list[pathlib.Path] = []
    if isinstance(bound, str) and pathlib.PurePath(bound).name == bound and bound.lower().endswith(".json"):
        path = inflight / bound
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                candidates.append(path)
            else:
                if payload.get("operation") == "reload_checkpoint":
                    candidates.append(path)
    else:
        for path in sorted(inflight.glob("*.json"), key=lambda item: item.name.lower()):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                candidates.append(path)
                continue
            if (
                payload.get("schema") == "oni.validation-control.v1"
                and payload.get("operation") == "reload_checkpoint"
                and payload.get("status") == "inflight"
            ):
                candidates.append(path)

    terminal: list[dict[str, Any]] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            control_id = path.stem if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", path.stem) else (
                "reload-recovery-" + hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:24]
            )
            payload = {
                "schema": "oni.validation-control-recovery.v1",
                "id": control_id,
                "operation": "reload_checkpoint",
                "status": "quarantined",
                "result": {"reason_code": "corrupt_reload_intent", "replayed": False},
            }
            target = quarantine / f"{control_id}.json"
        else:
            valid = (
                payload.get("schema") == "oni.validation-control.v1"
                and payload.get("operation") == "reload_checkpoint"
                and payload.get("status") == "inflight"
                and path.stem.startswith(str(payload.get("id") or ""))
            )
            payload["status"] = "failed" if valid else "quarantined"
            payload["result"] = {"reason_code": "corrupt_reload_intent", "replayed": False}
            target = (failed if valid else quarantine) / path.name
        write_json_atomic(target, payload)
        path.unlink()
        terminal.append({"control_id": payload["id"], "status": payload["status"]})

    if intent_path.exists():
        intent_path.replace(recovery / "reload-intent.json")
    write_json_atomic(
        recovery / "reload-recovery.json",
        {"status": "quarantined", "replayed": False, "terminal_controls": terminal},
    )
    return terminal


def verify_reload_recovery_fixtures() -> dict:
    reload_control = {
        "schema": "oni.validation-control.v1",
        "id": "reload-a",
        "operation": "reload_checkpoint",
        "status": "inflight",
    }
    save_control = {
        "schema": "oni.validation-control.v1",
        "id": "save-b",
        "operation": "save_checkpoint",
        "status": "inflight",
    }
    with tempfile.TemporaryDirectory(prefix="oni-corrupt-reload-") as temporary:
        root = pathlib.Path(temporary)
        (root / "inflight").mkdir(parents=True)
        (root / "reload-intent.json").write_bytes(b"{corrupt")
        write_json_atomic(root / "inflight" / "reload-a.json", reload_control)
        write_json_atomic(root / "inflight" / "save-b.json", save_control)
        (root / "inflight" / "broken.json").write_bytes(b"not-json")
        terminal = _offline_recover_reload(root)
        require([item["status"] for item in terminal] == ["quarantined", "failed"], "corrupt recovery order/status changed")
        require((root / "failed" / "reload-a.json").is_file(), "matching reload did not fail")
        require((root / "quarantine" / "broken.json").is_file(), "corrupt inflight was not quarantined")
        require((root / "inflight" / "save-b.json").is_file(), "unrelated save inflight was consumed")
        require(not (root / "reload-intent.json").exists(), "corrupt intent remained replayable")
        before = sorted(path.relative_to(root) for path in root.rglob("*.json"))
        require(_offline_recover_reload(root) == [], "restart re-terminalized a recovered control")
        after = sorted(path.relative_to(root) for path in root.rglob("*.json"))
        require(before == after, "restart changed an already recovered filesystem state")

    with tempfile.TemporaryDirectory(prefix="oni-bound-corrupt-reload-") as temporary:
        root = pathlib.Path(temporary)
        (root / "inflight").mkdir(parents=True)
        write_json_atomic(root / "reload-intent.json", {"inflight_file": "bound.json"})
        (root / "inflight" / "bound.json").write_bytes(b"not-json")
        write_json_atomic(root / "inflight" / "reload-a.json", reload_control)
        terminal = _offline_recover_reload(root)
        require(terminal == [{"control_id": "bound", "status": "quarantined"}], "bound corrupt payload was not isolated")
        require((root / "inflight" / "reload-a.json").is_file(), "readable intent consumed an unrelated reload")
    return {"corrupt_intent": True, "corrupt_inflight": True, "restart_idempotent": True}


def main() -> int:
    # The path identity must match the mod's normalized uppercase UTF-8 contract.
    sample = normalized_path(pathlib.Path("C:/Temp/Case.sav"))
    expected = "sha256:" + hashlib.sha256(str(sample).rstrip("\\/").upper().encode("utf-8")).hexdigest()
    require(path_id(sample) == expected, "path identity algorithm changed")
    report = {
        "schema": "oni.validation-checkpoint-verification.v1",
        "status": "passed",
        "policy": verify_policy_and_request(),
        "environment": verify_environment_gate(),
        "manager_confirmation": verify_manager_confirmation_contract(),
        "mod": verify_mod_contract(),
        "reload_recovery": verify_reload_recovery_contract(),
        "reload_recovery_fixtures": verify_reload_recovery_fixtures(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
