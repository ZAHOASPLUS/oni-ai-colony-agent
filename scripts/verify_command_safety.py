#!/usr/bin/env python3
"""Contract checks for command V3 policy, approval, dedupe, and executor gates."""

from __future__ import annotations

import datetime as dt
import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import command_queue, rules as agent_rules
from agent.advanced_actions import build_duplicant_priority_hint
from agent.command_queue import ALLOWED_ACTIONS, build_command, ensure_command_dirs, sync_pending_commands, write_json_atomic
from agent.decision import build_decision_frame
from agent.canonical_hash import canonical_hash
from agent.decision_frame_archive import persist_decision_frame_archive
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    build_condition,
    contract_quality_fixture,
)
from agent.incidents import IncidentTracker
from agent.state_quality import canonical_state_hash
from scripts.verify_decision_frame import healthy_inputs


SAVE_HASH = "sha256:" + "b" * 64
OTHER_SAVE_HASH = "sha256:" + "c" * 64


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("Windows PowerShell is unavailable")
    return executable


def run_ps(script: pathlib.Path, *arguments: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments],
        text=True,
        capture_output=True,
        # Windows PowerShell 5.1 writes localized errors in the console code
        # page while PYTHONUTF8=1 decodes as UTF-8; a decode failure in the
        # reader thread left stderr=None.
        errors="replace",
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if expect_success and process.returncode != 0:
        raise AssertionError((process.stderr or process.stdout)[-800:])
    if not expect_success and process.returncode == 0:
        raise AssertionError("Command unexpectedly succeeded")
    return process


def state(save_hash: str = SAVE_HASH) -> Dict[str, Any]:
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "session_id": "session-command-safety",
        "seq": 8,
        "snapshot_seq": 8,
        "snapshot_id": "session-command-safety:8",
        "save_id_hash": save_hash,
        "save_dirty": False,
        "world_id": 0,
        "game": {"paused": False},
        "research": {"current": "FarmingTech"},
        "bridge": {
            "stale": False,
            "replay_mode": False,
            "trust_ready": True,
            "trust_blockers": [],
        },
        "quality": {
            "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
            "research.current": contract_quality_fixture(RESEARCH_CURRENT_CONTRACT_ID),
        },
    }


def write_authorizing_frame(
    bridge_dir: pathlib.Path,
    snapshot: Dict[str, Any],
    *,
    survival_incident: Dict[str, Any] | None = None,
    survival_advice: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    values = healthy_inputs(actions_enabled=True, suggestions_enabled=True)
    values["state"] = copy.deepcopy(snapshot)
    if survival_incident is not None or survival_advice is not None:
        require(
            isinstance(survival_incident, dict) and isinstance(survival_advice, dict),
            "survival frame fixture requires both incident and advice",
        )
        values["incidents"]["items"] = [copy.deepcopy(survival_incident)]
        values["advice"]["items"] = [copy.deepcopy(survival_advice)]
    state_hash = canonical_state_hash(snapshot)
    identity = {
        "session_id": snapshot["session_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "state_hash": state_hash,
    }
    health = values["health"]
    health.update(
        {
            "status": "ok",
            "current_session_id": snapshot["session_id"],
            "latest_snapshot_id": snapshot["snapshot_id"],
            "latest_state_hash": state_hash,
            "source": copy.deepcopy(identity),
            "trust_ready": True,
            "replay_mode": False,
            "session_start_observed": True,
        }
    )
    for name in ("advice", "incidents", "plan", "strategy"):
        values[name]["source"] = copy.deepcopy(identity)
    values["advice"]["session_id"] = snapshot["session_id"]
    values["incidents"]["session_id"] = snapshot["session_id"]
    for item in values["advice"].get("items") or []:
        item["source_snapshot_id"] = snapshot["snapshot_id"]
        item["state_hash"] = state_hash
    for item in values["incidents"].get("items") or []:
        item["snapshot_id"] = snapshot["snapshot_id"]
        item["state_hash"] = state_hash
    for item in values["plan"].get("plan") or []:
        item["source_snapshot_id"] = snapshot["snapshot_id"]
        item["state_hash"] = state_hash
    strategy = values["strategy"]
    strategy.update(
        {
            "session_id": snapshot["session_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_seq": snapshot.get("snapshot_seq"),
            "seq": snapshot["seq"],
            "ts": snapshot["ts"],
            "state_hash": state_hash,
        }
    )
    policy = json.loads((bridge_dir / "action-policy.json").read_text(encoding="utf-8-sig"))
    policy["source"] = copy.deepcopy(identity)
    values["policy"] = policy
    frame = build_decision_frame(
        values["state"],
        values["health"],
        values["advice"],
        values["incidents"],
        values["plan"],
        values["strategy"],
        values["policy"],
    )
    require(frame["readiness"]["propose"]["ready"] is True, "fixture frame cannot propose")
    write_json_atomic(bridge_dir / "decision-frame.json", frame)
    persist_decision_frame_archive(bridge_dir, frame)
    return frame


def certified_suffocation_context(
    snapshot: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build one immediate pause episode through the production rule/tracker path."""

    snapshot["alerts"] = ["suffocation"]
    snapshot["quality"]["alerts"] = contract_quality_fixture(
        ALERTS_CANONICAL_CONTRACT_ID
    )
    signal = next(
        item
        for item in agent_rules.evaluate_signals(snapshot)
        if item.get("incident_id") == "suffocation"
        and item.get("state") == "active"
    )
    tracker = IncidentTracker()
    tracker.update(snapshot, [signal])
    incident = copy.deepcopy(tracker.incidents["suffocation"])
    require(incident.get("status") == "open", "suffocation fixture did not open")
    item = {
        "incident_id": signal["incident_id"],
        "incident_opened_at": incident["first_seen"],
        "risk": signal["risk"],
        "title": signal["title"],
        "reason": signal["reason"],
        "evidence": copy.deepcopy(signal["evidence"]),
        "source_snapshot_id": snapshot["snapshot_id"],
        "command": copy.deepcopy(signal["command"]),
    }
    return incident, item


def advice() -> Dict[str, Any]:
    return {
        "incident_id": "command_safety_pause",
        "incident_opened_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "risk": "low",
        "title": "Pause contract check",
        "reason": "Synthetic command safety scenario",
        "evidence": {"game.paused": False},
        "source_snapshot_id": "session-command-safety:8",
        "command": {
            "action": "set_paused",
            "args": {"paused": True},
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
        },
    }


def write_pending(bridge_dir: pathlib.Path, command: Dict[str, Any]) -> pathlib.Path:
    dirs = ensure_command_dirs(bridge_dir)
    path = dirs["pending"] / f"{command['id']}.json"
    write_json_atomic(path, command)
    return path


def write_policy(
    bridge_dir: pathlib.Path,
    *,
    enabled: bool,
    save_hash: str | None = SAVE_HASH,
    suggestions_enabled: bool = True,
) -> subprocess.CompletedProcess[str]:
    script = PROJECT_ROOT / "scripts" / "write-action-policy.ps1"
    arguments = ["-BridgeDir", str(bridge_dir), "-AllowedActions", "set_paused"]
    if save_hash:
        arguments.extend(["-AllowedSaveHashes", save_hash])
    if enabled:
        arguments.append("-EnableActions")
    if not suggestions_enabled:
        arguments.append("-DisableSuggestions")
    return run_ps(script, *arguments)


def case_policy_fail_closed(root: pathlib.Path) -> Dict[str, Any]:
    bridge = root / "policy"
    write_policy(bridge, enabled=False)
    policy = json.loads((bridge / "action-policy.json").read_text(encoding="utf-8-sig"))
    require(policy["actions_enabled"] is False, "default action policy was not disabled")

    failed = run_ps(
        PROJECT_ROOT / "scripts" / "write-action-policy.ps1",
        "-BridgeDir",
        str(root / "missing-hash"),
        "-AllowedActions",
        "set_paused",
        "-EnableActions",
        expect_success=False,
    )
    require("save" in (failed.stderr + failed.stdout).lower(), "missing-save failure did not explain the gate")
    advanced_bridge = root / "advanced-policy"
    run_ps(
        PROJECT_ROOT / "scripts" / "write-action-policy.ps1",
        "-BridgeDir",
        str(advanced_bridge),
        "-AllowedActions",
        "set_duplicant_chore_priority",
    )
    advanced_policy = json.loads((advanced_bridge / "action-policy.json").read_text(encoding="utf-8-sig"))
    require(
        advanced_policy["allowed_actions"] == ["set_duplicant_chore_priority"],
        "batch-A action was not accepted by the policy writer",
    )
    batch_b_actions = ("mark_dig_cells", "cancel_dig_cells")
    for action in batch_b_actions:
        action_bridge = root / f"batch-b-policy-{action}"
        run_ps(
            PROJECT_ROOT / "scripts" / "write-action-policy.ps1",
            "-BridgeDir",
            str(action_bridge),
            "-AllowedActions",
            action,
        )
        action_policy = json.loads((action_bridge / "action-policy.json").read_text(encoding="utf-8-sig"))
        require(action_policy["allowed_actions"] == [action], f"{action} was not accepted by the policy writer")
    batch_c_actions = (
        "place_building_blueprint",
        "cancel_building_blueprint",
        "place_utility_blueprint_path",
        "cancel_utility_blueprint_path",
    )
    for action in batch_c_actions:
        action_bridge = root / f"batch-c-policy-{action}"
        run_ps(
            PROJECT_ROOT / "scripts" / "write-action-policy.ps1",
            "-BridgeDir",
            str(action_bridge),
            "-AllowedActions",
            action,
        )
        action_policy = json.loads((action_bridge / "action-policy.json").read_text(encoding="utf-8-sig"))
        require(action_policy["allowed_actions"] == [action], f"{action} was not accepted by the policy writer")
    batch_d_controls = {
        "create_colony_plan",
        "advance_colony_plan",
        "pause_colony_plan",
        "resume_colony_plan",
        "abort_colony_plan",
        "recover_colony_plan",
    }
    require(batch_d_controls.isdisjoint(ALLOWED_ACTIONS), "bridge plan controls entered the game action allowlist")
    policy_source = (PROJECT_ROOT / "scripts" / "write-action-policy.ps1").read_text(encoding="utf-8")
    require(
        all(f'"{action}"' not in policy_source for action in batch_d_controls),
        "bridge plan controls entered the game action policy writer",
    )
    return {
        "default_enabled": policy["actions_enabled"],
        "enable_without_save_rejected": True,
        "batch_a_policy_action_supported": True,
        "batch_b_policy_actions_supported": True,
        "batch_c_policy_actions_supported": True,
        "batch_d_controls_excluded_from_game_policy": True,
    }


def case_approval_gates(root: pathlib.Path) -> Dict[str, Any]:
    approval_script = PROJECT_ROOT / "scripts" / "approve-command.ps1"

    valid_bridge = root / "valid"
    write_policy(valid_bridge, enabled=True)
    valid_state = state()
    valid_incident, valid_advice = certified_suffocation_context(valid_state)
    valid_frame = write_authorizing_frame(
        valid_bridge,
        valid_state,
        survival_incident=valid_incident,
        survival_advice=valid_advice,
    )
    valid_policy = json.loads((valid_bridge / "action-policy.json").read_text(encoding="utf-8-sig"))
    valid_command = build_command(
        valid_state,
        valid_advice,
        valid_advice["command"],
        ttl_seconds=30,
        source_frame_id=valid_frame["frame_id"],
        source_policy_hash=canonical_hash(valid_policy),
    )
    write_pending(valid_bridge, valid_command)
    run_ps(approval_script, "-BridgeDir", str(valid_bridge), "-CommandId", valid_command["id"])
    approved_path = valid_bridge / "commands" / "approved" / f"{valid_command['id']}.json"
    approved = json.loads(approved_path.read_text(encoding="utf-8-sig"))
    require(approved["status"] == "approved", "valid command was not moved to approved")
    require(approved["approved_by"] == "local_human" and approved.get("approved_at"), "approval audit fields are missing")
    require(not (valid_bridge / "commands" / "pending" / approved_path.name).exists(), "approved command remained pending")

    legacy_mutation_bridge = root / "legacy-mutation"
    write_policy(legacy_mutation_bridge, enabled=True)
    legacy_mutation = build_command(state(), advice(), advice()["command"], ttl_seconds=30)
    legacy_mutation["id"] = "cmd-legacy-v2-mutation"
    legacy_mutation["schema"] = "oni.command.v2"
    legacy_mutation_path = write_pending(legacy_mutation_bridge, legacy_mutation)
    run_ps(
        approval_script,
        "-BridgeDir",
        str(legacy_mutation_bridge),
        "-CommandId",
        legacy_mutation["id"],
        expect_success=False,
    )
    require(legacy_mutation_path.exists(), "legacy v2 mutation moved despite failed approval")

    expired_bridge = root / "expired"
    write_policy(expired_bridge, enabled=True)
    expired = build_command(state(), advice(), advice()["command"], ttl_seconds=30)
    expired["id"] = "cmd-expired-contract"
    expired["expires_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
    expired_path = write_pending(expired_bridge, expired)
    run_ps(approval_script, "-BridgeDir", str(expired_bridge), "-CommandId", expired["id"], expect_success=False)
    require(expired_path.exists(), "expired command was moved despite failed approval")

    wrong_save_bridge = root / "wrong-save"
    write_policy(wrong_save_bridge, enabled=True)
    wrong_save = build_command(state(OTHER_SAVE_HASH), advice(), advice()["command"], ttl_seconds=30)
    wrong_save["id"] = "cmd-wrong-save-contract"
    wrong_save_path = write_pending(wrong_save_bridge, wrong_save)
    run_ps(approval_script, "-BridgeDir", str(wrong_save_bridge), "-CommandId", wrong_save["id"], expect_success=False)
    require(wrong_save_path.exists(), "wrong-save command was moved despite failed approval")

    disabled_bridge = root / "disabled"
    write_policy(disabled_bridge, enabled=False)
    disabled = build_command(state(), advice(), advice()["command"], ttl_seconds=30)
    disabled["id"] = "cmd-disabled-contract"
    disabled_path = write_pending(disabled_bridge, disabled)
    run_ps(approval_script, "-BridgeDir", str(disabled_bridge), "-CommandId", disabled["id"], expect_success=False)
    require(disabled_path.exists(), "disabled-policy command was moved despite failed approval")
    return {
        "valid_approved": True,
        "legacy_v2_mutation_rejected": True,
        "expired_rejected": True,
        "wrong_save_rejected": True,
        "disabled_policy_rejected": True,
    }


def case_dedupe_and_envelope(root: pathlib.Path) -> Dict[str, Any]:
    bridge = root / "dedupe"
    write_policy(bridge, enabled=False)
    snapshot = state()
    incident = advice()
    frame = write_authorizing_frame(bridge, snapshot)
    first = sync_pending_commands(bridge, snapshot, [incident], proposal_frame=frame)
    second = sync_pending_commands(bridge, snapshot, [incident], proposal_frame=frame)
    require(len(first) == 1 and len(second) == 0, "same incident created duplicate pending commands")
    command = first[0]
    require(command["schema"] == "oni.command.v3", "generated command does not use V3 schema")
    require(command["session_id"] == snapshot["session_id"], "session binding is missing")
    require(command["save_id_hash"] == snapshot["save_id_hash"], "save binding is missing")
    require(command["world_id"] == snapshot["world_id"], "world binding is missing")
    require(command["source_snapshot_id"] == snapshot["snapshot_id"], "snapshot binding is missing")
    require(str(command["state_hash"]).startswith("sha256:"), "state audit hash is missing")
    require(command["source_frame_id"] == frame["frame_id"], "decision frame binding is missing")
    require(command["preconditions"] and command["expected_effects"], "state conditions are missing")
    return {"first_created": len(first), "second_created": len(second), "schema": command["schema"]}


def case_dynamic_condition_contracts(root: pathlib.Path) -> Dict[str, Any]:
    del root
    snapshot = state()
    snapshot["game"]["paused"] = True
    priority_path = "duplicants.by_instance_id.id_123.chore_priorities.Dig"
    snapshot["duplicants"] = {
        "by_instance_id": {
            "id_123": {
                "instance_id": 123,
                "name": "Ada",
                "world_id": 0,
                "chore_priorities": {"Dig": 2},
            }
        }
    }
    snapshot["quality"][priority_path] = contract_quality_fixture(
        DUPLICANT_CHORE_PRIORITY_CONTRACT_ID
    )
    hint = build_duplicant_priority_hint(
        snapshot,
        instance_id=123,
        chore_group="Dig",
        priority=4,
    )
    item = {"source_snapshot_id": snapshot["snapshot_id"], "command": hint}
    require(
        command_queue._advice_command_is_current_and_valid(snapshot, item),
        "correct dynamic V3 condition contract was rejected",
    )

    tampered_state = json.loads(json.dumps(snapshot))
    tampered_state["quality"][priority_path]["semantic"] = "priority_guess"
    require(
        not command_queue._advice_command_is_current_and_valid(tampered_state, item),
        "tampered dynamic quality metadata authorized a command",
    )

    unknown_contract = json.loads(json.dumps(item))
    unknown_contract["command"]["expected_effects"][0]["quality_contract_id"] = "oni.qc.unknown.v1"
    require(
        not command_queue._advice_command_is_current_and_valid(snapshot, unknown_contract),
        "unknown expected-effect quality contract authorized a command",
    )

    malicious_operator = json.loads(json.dumps(item))
    malicious_operator["command"]["expected_effects"][0]["op"] = "evil"
    require(
        not command_queue._advice_command_is_current_and_valid(snapshot, malicious_operator),
        "unknown expected-effect operator authorized a command",
    )

    wrong_dynamic_type = json.loads(json.dumps(item))
    wrong_dynamic_type["command"]["expected_effects"][0]["value"] = True
    require(
        not command_queue._advice_command_is_current_and_valid(snapshot, wrong_dynamic_type),
        "boolean spoof entered an integer dynamic effect",
    )
    return {
        "correct_dynamic_contract_accepted": True,
        "tampered_metadata_rejected": True,
        "unknown_contract_rejected": True,
        "unknown_operator_rejected": True,
        "dynamic_type_spoof_rejected": True,
    }


def _decision_surface_fixture() -> Dict[str, Any]:
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "ts": "2026-08-30T17:00:00+08:00",
        "session_id": "session-hash-contract",
        "seq": 21,
        "snapshot_id": "session-hash-contract:21",
        "game_build": "cl-744825",
        "mod_version": "0.7.0",
        "save_id_hash": SAVE_HASH,
        "save_dirty": False,
        "world_id": 0,
        "cycle": 31,
        "game": {"paused": True},
        "duplicants": {"total": 5},
        "resources": {"food_kcal": 18000.0, "water_kg": 9000.0},
        "alerts": ["low_oxygen"],
        "research": {"current": "FarmingTech"},
        "buildings": {"oxygen_diffuser": 1},
        "power": {"stored_joules": 240000.0},
        "temperature": {"habitat_max_c": 29.0},
        "production": {"oxygen_kg_per_cycle": 420.0},
        "rates": {"food_kcal_delta_per_cycle": 1500.0},
        "rockets": {"ready": 0},
        "space": {"discovered_destinations": 0},
        "colony": {"phase": "stabilize"},
        "sustainability": {"renewable_oxygen": False},
        "designations": {"dig": {"candidate_cells": [456]}},
        "blueprints": {"by_instance_id": {"id_9001": {"cell": 500}}},
        "cells": {"by_id": {"cell_500": {"fingerprint": "sha256:" + "a" * 64}}},
        "action_targets": {"priority_duplicant_ids": [123]},
        "quality": {"power.stored_joules": {"status": "observed", "source": "fixture"}},
        "sample": {"interval_seconds": 5.0, "duration_ms": 2.1},
        "writer": {"queue_depth": 1, "dropped_snapshot_count": 0},
        "read_metrics_ms": {"power": 0.3},
        "bridge": {"stale": False, "snapshot_age_seconds": 0.4},
        "new_telemetry": {"fault_domains": {"oxygen": "single_point"}},
    }


def _reverse_mapping_order(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reverse_mapping_order(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_mapping_order(item) for item in value]
    return value


def case_state_hash_decision_surface(root: pathlib.Path) -> Dict[str, Any]:
    del root
    baseline = _decision_surface_fixture()
    baseline_hash = canonical_state_hash(baseline)
    mutations: Dict[str, Callable[[Dict[str, Any]], None]] = {
        "schema": lambda value: value.__setitem__("schema", "oni.state.v3"),
        "event": lambda value: value.__setitem__("event", "different_snapshot"),
        "timestamp": lambda value: value.__setitem__("ts", "2026-08-30T17:00:01+08:00"),
        "session_identity": lambda value: value.__setitem__("session_id", "session-hash-other"),
        "sequence_identity": lambda value: value.__setitem__("seq", 22),
        "snapshot_identity": lambda value: value.__setitem__("snapshot_id", "session-hash-contract:22"),
        "build_identity": lambda value: value.__setitem__("game_build", "cl-744826"),
        "mod_identity": lambda value: value.__setitem__("mod_version", "0.6.9"),
        "save_identity": lambda value: value.__setitem__("save_id_hash", OTHER_SAVE_HASH),
        "save_dirty": lambda value: value.__setitem__("save_dirty", True),
        "world_identity": lambda value: value.__setitem__("world_id", 1),
        "cycle": lambda value: value.__setitem__("cycle", 32),
        "game": lambda value: value["game"].__setitem__("paused", False),
        "duplicants": lambda value: value["duplicants"].__setitem__("total", 6),
        "resources": lambda value: value["resources"].__setitem__("food_kcal", 17999.0),
        "alerts": lambda value: value["alerts"].append("starvation"),
        "research": lambda value: value["research"].__setitem__("current", "FineDining"),
        "buildings": lambda value: value["buildings"].__setitem__("oxygen_diffuser", 2),
        "power": lambda value: value["power"].__setitem__("stored_joules", 239999.0),
        "temperature": lambda value: value["temperature"].__setitem__("habitat_max_c", 30.0),
        "production": lambda value: value["production"].__setitem__("oxygen_kg_per_cycle", 419.0),
        "rates": lambda value: value["rates"].__setitem__("food_kcal_delta_per_cycle", -50.0),
        "rockets": lambda value: value["rockets"].__setitem__("ready", 1),
        "space": lambda value: value["space"].__setitem__("discovered_destinations", 1),
        "colony": lambda value: value["colony"].__setitem__("phase", "expand"),
        "sustainability": lambda value: value["sustainability"].__setitem__("renewable_oxygen", True),
        "designations": lambda value: value["designations"]["dig"]["candidate_cells"].append(457),
        "blueprints": lambda value: value["blueprints"]["by_instance_id"]["id_9001"].__setitem__("cell", 501),
        "cells": lambda value: value["cells"]["by_id"]["cell_500"].__setitem__("fingerprint", "sha256:" + "b" * 64),
        "action_targets": lambda value: value["action_targets"]["priority_duplicant_ids"].append(124),
        "quality": lambda value: value["quality"]["power.stored_joules"].__setitem__("status", "estimated"),
        "sample_interval": lambda value: value["sample"].__setitem__("interval_seconds", 10.0),
        "new_telemetry": lambda value: value["new_telemetry"]["fault_domains"].__setitem__("oxygen", "redundant"),
    }
    changed_domains: List[str] = []
    for name, mutate in mutations.items():
        candidate = json.loads(json.dumps(baseline))
        mutate(candidate)
        require(canonical_state_hash(candidate) != baseline_hash, f"state hash ignored decision domain: {name}")
        changed_domains.append(name)

    reordered = _reverse_mapping_order(baseline)
    require(canonical_state_hash(reordered) == baseline_hash, "dictionary key order changed state hash")

    transport_only = json.loads(json.dumps(baseline))
    transport_only["bridge"] = {"stale": True, "snapshot_age_seconds": 999.0}
    transport_only["writer"] = {"queue_depth": 999, "dropped_snapshot_count": 12}
    transport_only["read_metrics_ms"] = {"power": 99.0}
    transport_only["sample"]["duration_ms"] = 99.0
    require(
        canonical_state_hash(transport_only) == baseline_hash,
        "explicitly excluded capture/transport diagnostics changed state hash",
    )
    require(baseline["bridge"]["snapshot_age_seconds"] == 0.4, "state hash mutated its input")
    return {
        "changed_domains": changed_domains,
        "changed_domain_count": len(changed_domains),
        "key_order_stable": True,
        "excluded_transport_diagnostics": True,
    }


def _synthetic_winerror(code: int) -> OSError:
    error = OSError(f"synthetic WinError {code}")
    error.winerror = code  # type: ignore[attr-defined]
    return error


def case_atomic_command_write(root: pathlib.Path) -> Dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    original_replace = pathlib.Path.replace
    transient_target = root / "transient.json"
    transient_calls = 0

    def transient_replace(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
        nonlocal transient_calls
        transient_calls += 1
        if transient_calls <= 2:
            raise _synthetic_winerror(32)
        return original_replace(source, destination)

    with (
        mock.patch.object(pathlib.Path, "replace", new=transient_replace),
        mock.patch.object(command_queue.time, "sleep", return_value=None) as transient_sleep,
    ):
        write_json_atomic(transient_target, {"status": "new"})
    require(transient_calls == 3, "transient lock did not retry to success")
    require(transient_sleep.call_count == 2, "transient retry backoff count is wrong")
    require(json.loads(transient_target.read_text(encoding="utf-8")) == {"status": "new"}, "retry wrote wrong payload")

    persistent_target = root / "persistent.json"
    write_json_atomic(persistent_target, {"status": "old"})
    with (
        mock.patch.object(pathlib.Path, "replace", side_effect=_synthetic_winerror(33)) as persistent_replace,
        mock.patch.object(command_queue.time, "sleep", return_value=None) as persistent_sleep,
    ):
        try:
            write_json_atomic(persistent_target, {"status": "blocked"})
        except OSError:
            pass
        else:
            raise AssertionError("persistent destination lock unexpectedly succeeded")
    require(
        persistent_replace.call_count == command_queue._ATOMIC_REPLACE_ATTEMPTS,
        "persistent lock exceeded or skipped the bounded retry budget",
    )
    require(
        persistent_sleep.call_count == command_queue._ATOMIC_REPLACE_ATTEMPTS - 1,
        "persistent lock backoff count is wrong",
    )
    require(
        json.loads(persistent_target.read_text(encoding="utf-8")) == {"status": "old"},
        "failed replace damaged the old command file",
    )

    non_retryable_target = root / "non-retryable.json"
    write_json_atomic(non_retryable_target, {"status": "old"})
    with (
        mock.patch.object(pathlib.Path, "replace", side_effect=FileNotFoundError("synthetic missing path")) as non_retryable,
        mock.patch.object(command_queue.time, "sleep", return_value=None) as non_retryable_sleep,
    ):
        try:
            write_json_atomic(non_retryable_target, {"status": "missing"})
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("non-retryable replace error unexpectedly succeeded")
    require(non_retryable.call_count == 1, "non-retryable replace error was retried")
    require(non_retryable_sleep.call_count == 0, "non-retryable replace error slept")
    require(
        json.loads(non_retryable_target.read_text(encoding="utf-8")) == {"status": "old"},
        "non-retryable failure damaged the old command file",
    )
    require(not list(root.glob("*.tmp")), "atomic command write left temporary files")
    return {
        "transient_replace_calls": transient_calls,
        "persistent_replace_calls": persistent_replace.call_count,
        "non_retryable_replace_calls": non_retryable.call_count,
        "old_targets_preserved": True,
        "temporary_files_cleaned": True,
    }


def case_suggestion_gate_preserves_operator_commands(root: pathlib.Path) -> Dict[str, Any]:
    bridge = root / "operator-pending"
    write_policy(bridge, enabled=True, suggestions_enabled=False)
    snapshot = state()

    suggested = build_command(snapshot, advice(), advice()["command"], ttl_seconds=30)
    suggested["id"] = "cmd-suggested-disabled"

    runtime_validation = json.loads(json.dumps(suggested))
    runtime_validation.update(
        {
            "id": "cmd-runtime-validation-pending",
            "mode": "runtime_validation",
            "source_incident_id": None,
            "source": {"kind": "runtime_validation"},
        }
    )
    planned = json.loads(json.dumps(runtime_validation))
    planned.update(
        {
            "id": "cmd-planned-pending",
            "mode": "planned",
            "source": {"kind": "colony_plan"},
        }
    )

    suggested_path = write_pending(bridge, suggested)
    runtime_path = write_pending(bridge, runtime_validation)
    planned_path = write_pending(bridge, planned)
    sync_pending_commands(bridge, snapshot, [])

    require(not suggested_path.exists(), "disabled suggestions left a suggested command pending")
    require(runtime_path.exists(), "disabled suggestions removed a runtime validation command")
    require(planned_path.exists(), "disabled suggestions removed a planned command")
    rejected = json.loads(
        (bridge / "commands" / "rejected" / "cmd-suggested-disabled.json").read_text(encoding="utf-8-sig")
    )
    require(rejected.get("rejection_reason") == "suggestions_disabled", "suggested rejection reason is wrong")

    expired = json.loads(json.dumps(runtime_validation))
    expired["id"] = "cmd-runtime-validation-expired"
    expired["expires_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
    expired_path = write_pending(bridge, expired)
    sync_pending_commands(bridge, snapshot, [])
    require(not expired_path.exists(), "expired runtime validation command remained pending")
    expired_rejected = json.loads(
        (bridge / "commands" / "rejected" / "cmd-runtime-validation-expired.json").read_text(encoding="utf-8-sig")
    )
    require(expired_rejected.get("rejection_reason") == "expired_before_approval", "operator TTL gate was bypassed")
    return {
        "suggested_rejected": True,
        "runtime_validation_preserved": True,
        "planned_preserved": True,
        "operator_ttl_enforced": True,
    }


def case_executor_static_contract(root: pathlib.Path) -> Dict[str, Any]:
    del root
    source = (PROJECT_ROOT / "mod" / "Source" / "Commands" / "ActionExecutor.cs").read_text(encoding="utf-8")
    claim_source = (PROJECT_ROOT / "mod" / "Source" / "Commands" / "ExecutionClaimStore.cs").read_text(
        encoding="utf-8"
    )
    prepared_recovery_source = (
        PROJECT_ROOT / "mod" / "Source" / "Commands" / "PreparedConfirmationRecovery.cs"
    ).read_text(encoding="utf-8")
    claim_harness_source = (
        PROJECT_ROOT / "tools" / "ExecutionClaimHarness" / "Program.cs"
    ).read_text(encoding="utf-8")
    canonical_source = (PROJECT_ROOT / "mod" / "Source" / "Security" / "CanonicalHashContract.cs").read_text(
        encoding="utf-8"
    )
    probe_source = (PROJECT_ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs").read_text(
        encoding="utf-8"
    )
    writer_source = (PROJECT_ROOT / "mod" / "Source" / "Probe" / "ProbeWriter.cs").read_text(
        encoding="utf-8"
    )
    session_source = (PROJECT_ROOT / "mod" / "Source" / "Probe" / "ProbeSession.cs").read_text(
        encoding="utf-8"
    )
    lifecycle_source = (PROJECT_ROOT / "mod" / "Source" / "Patches" / "LifecyclePatches.cs").read_text(
        encoding="utf-8"
    )
    lineage_source = (PROJECT_ROOT / "scripts" / "manage_save_lineage.py").read_text(encoding="utf-8")
    required_fragments = [
        '"oni.command.v3"',
        '"oni.command.v2"',
        '"actions_enabled"',
        "ProbeSession.CurrentSessionId",
        "ProbeSession.IsSaveDirty",
        '"expires_at"',
        "HasProcessedId",
        "QuarantineStartupCommands",
        "RecoverExecutingAtStartup",
        "ExecutionClaimStore.TryClaimApproved",
        "dirs.Approving",
        "dirs.Executing",
        '"accepted"',
        '"executed"',
        '"local_effect_observed"',
        '"executed_pending_snapshot"',
        "PollWitnessConfirmation",
        "pendingWitnessId",
        "ValidateApprovalReceipt",
        '"oni.approval-receipt.v1"',
        "VerifyPendingCommands",
        "created_at is unreasonably far in the future.",
        "CommandClockSkewSeconds",
        "HasMutatingVerificationInFlight",
        "mutationSafetyLatch",
        "VerificationContextStillValid",
        "AcceptedSaveWriteGeneration",
        "JToken.DeepEquals(pending.AcceptedPolicy, policy)",
        "FinalizeOutstandingAsUnknown",
        '"outcome_unknown"',
        'ProbeWriter.WriteEvent("command_safety_latched"',
        "SpeedControlScreen.Instance",
        "Research.Instance",
        "SetActiveResearch(definition, true)",
        "FloatEqualityRelativeTolerance",
        "left.Type == JTokenType.Integer || right.Type == JTokenType.Integer",
        'case "set_duplicant_chore_priority"',
        "consumer.GetPersonalPriority(group)",
        "consumer.SetPersonalPriority(group, priority)",
        'ContainsObservedEquality(preconditions, "game.paused"',
        'case "mark_dig_cells"',
        'case "cancel_dig_cells"',
        "DigTool.PlaceDig(cell, created.Count)",
        "designation.gameObject.Trigger(CancelEventHash)",
        "ValidateDigConditionContract",
        'case "place_building_blueprint"',
        'case "cancel_building_blueprint"',
        'case "place_utility_blueprint_path"',
        'case "cancel_utility_blueprint_path"',
        "def.TryPlace(",
        "ValidateBlueprintConditionContract",
        "CancelCreatedBlueprints(pending.CreatedBlueprints)",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in source]
    require(not missing, "executor is missing safety fragments: " + ", ".join(missing))
    require(
        "CanonicalHashContract.ParseObjectStrict" in source
        and "DateParseHandling.None" in canonical_source
        and "FloatParseHandling.Double" in canonical_source,
        "executor does not use the shared strict JSON parser at the approval/execution boundary",
    )
    require(
        "approving" in command_queue.COMMAND_DIRS
        and "executing" in command_queue.COMMAND_DIRS
        and command_queue.COMMAND_DIRS.index("pending") < command_queue.COMMAND_DIRS.index("approving")
        < command_queue.COMMAND_DIRS.index("approved") < command_queue.COMMAND_DIRS.index("executing")
        < command_queue.COMMAND_DIRS.index("confirming"),
        "Python command directories omit or misorder approval/execution claim states",
    )
    for fragment in (
        "FileOptions.WriteThrough",
        "stream.Flush(true)",
        "File.Replace(temp, path, backup)",
        "File.Move(approvedPath, executingPath)",
        "TryTransitionExecuting",
        '"atomic_move_no_directory_flush_barrier"',
        '"oni.execution-claim.v1"',
        '"async_writer_queue_unconfirmed"',
    ):
        require(fragment in claim_source, f"execution claim store is missing durability fragment {fragment}")
    execute_file = source[
        source.index("private static void ExecuteFile") : source.index("private static ValidationResult ValidateCommand")
    ]
    poll = source[
        source.index("private static void PollApprovedCommands") : source.index("private static void ExecuteFile")
    ]
    require(
        "Directory.GetFiles(dirs.Approved" in poll
        and "dirs.Approving" not in poll
        and "dirs.Executing" not in poll,
        "executor polls a pre-approval or already-claimed directory",
    )
    claim_index = execute_file.index("ExecutionClaimStore.TryClaimApproved")
    accepted_index = execute_file.index("long acceptedAckSeq = Ack")
    dispatch_index = execute_file.index("if (!ExecuteCommand")
    accepted_persist_index = execute_file.index("PersistExecutionClaim", accepted_index)
    require(
        claim_index < accepted_index < accepted_persist_index < dispatch_index,
        "mutating execution is not durably claimed with accepted ACK metadata before dispatch",
    )
    require(
        "durable session sequence" not in execute_file
        and "ExecutionClaimStore.AsyncAckDurability" in execute_file,
        "the async ProbeWriter ACK is being represented as durable",
    )
    require(
        execute_file.index("HasMutatingVerificationInFlight") < execute_file.index("ValidateCommand(command"),
        "a second mutating command can reach validation while another verification is in flight",
    )
    session_latch = source[
        source.index("private static void SetSessionLatch") : source.index(
            "private static void LatchFromUnresolvedFailures"
        )
    ]
    require(
        "mutationSafetyLatch = true" in session_latch,
        "SetSessionLatch does not engage the mutation latch",
    )
    startup = source[
        source.index("private static void QuarantineStartupCommands") : source.index(
            "private static void PollApprovedCommands"
        )
    ]
    approving_recovery = startup[
        startup.index("Directory.GetFiles(dirs.Approving") : startup.index(
            "Directory.GetFiles(dirs.Approved"
        )
    ]
    require(
        "QuarantineFile" in approving_recovery
        and '"skipped_stale"' in approving_recovery
        and "mutationSafetyLatch" not in approving_recovery
        and "PauseForUnknownOutcome" not in approving_recovery,
        "interrupted approval claims incorrectly engage mutation recovery",
    )
    require(
        "Directory.GetFiles(dirs.Executing" in startup
        and "RecoverExecutingAtStartup" in startup
        and "SetSessionLatch();" in startup
        and "PauseForUnknownOutcome" in startup
        and '"outcome_unknown"' in startup
        and "dirs.Failed" in startup,
        "startup does not fail every surviving executing claim closed",
    )
    prepared_index = startup.index("PreparedConfirmationRecovery.TryRecognize")
    ordinary_unknown_index = startup.index(
        'string confirmingReason =',
        prepared_index,
    )
    prepared_branch = startup[prepared_index:ordinary_unknown_index]
    require(
        'ReadString(command, "status"), "confirmed"' in startup
        and "pendingWitnessId = commandId" in prepared_branch
        and '"success_claimed", false' in prepared_branch
        and '"mutation_locked", true' in prepared_branch
        and "continue;" in prepared_branch
        and prepared_index < ordinary_unknown_index,
        "startup destroys or falsely completes a prepared-confirmed Bridge transaction",
    )
    for fragment in (
        'JournalSchema = "oni.action-outcomes-commit.v1"',
        '"action-outcomes-commit.json"',
        '"action-outcomes.json"',
        '"sessions"',
        'HashFieldMatches(journal, "transaction_hash")',
        'HashFieldMatches(document, "outcomes_hash")',
        'HashFieldMatches(confirmation, "attestation_hash")',
        "JToken.DeepEquals(rootDocument, sessionDocument)",
        "JToken.DeepEquals(outcome, embeddedConfirmation)",
        "ExecutionClaimStore.ValidateClaim",
        "WasRenamedToDone",
    ):
        require(
            fragment in prepared_recovery_source,
            f"prepared-confirmation recovery gate is missing {fragment}",
        )
    require(
        "CrashAfterBridgePrepareBeforeRename" in claim_harness_source
        and "startup recognition destroyed the prepared confirmation" in claim_harness_source
        and "startup recognition claimed success" in claim_harness_source
        and "Bridge journal recovery did not release the mutation lock" in claim_harness_source,
        "C# crash-boundary harness omits prepared-confirmation recovery",
    )
    require(
        "BridgeRenameDuringStartupEnumeration" in claim_harness_source
        and "PreparedConfirmationRecovery.WasRenamedToDone" in startup
        and '"bridge_rename_observed_pending_validation"' in startup,
        "startup can misclassify Bridge winning the confirming-to-done rename race",
    )
    verification = source[
        source.index("private static void VerifyPendingCommands") : source.index("private static void FinishPendingFailure")
    ]
    require(
        "mutating ? dirs.Confirming : dirs.Done" in verification
        and 'mutating ? "executed_pending_snapshot" : "verified"' in verification,
        "non-mutating commands still depend on trusted snapshot confirmation",
    )
    require(
        verification.index("VerificationContextStillValid") < verification.index("CaptureCurrentState"),
        "post-action state can be accepted before identity and policy continuity are checked",
    )
    context = source[
        source.index("private static bool VerificationContextStillValid") : source.index(
            "private static void FinalizeOutstandingAsUnknown"
        )
    ]
    for binding in (
        "ProbeSession.CurrentSessionId",
        "ProbeSession.CurrentSaveIdHash",
        "ProbeSession.CurrentWorldId",
        "ProbeSession.IsSaveDirty",
        "ProbeSession.SaveWriteGeneration",
        "JToken.DeepEquals",
    ):
        require(binding in context, f"verification continuity omits {binding}")
    unknown = source[
        source.index("private static void FinishPendingUnknown") : source.index(
            "private static bool VerifyActionSpecific"
        )
    ]
    require(
        "SetSessionLatch();" in unknown
        and "PauseForUnknownOutcome" in unknown
        and '"outcome_unknown"' in unknown
        and "EnsureCommandDirectories().Failed" in unknown,
        "unknown post-action outcomes do not durably fail closed and pause",
    )
    timestamp_validation = source[
        source.index('TryReadTimestamp(command, "created_at"') : source.index("HasProcessedId(dirs, id)")
    ]
    require(
        "createdAt > currentTime.AddSeconds(CommandClockSkewSeconds)" in timestamp_validation
        and "approvedAt < createdAt || expiresAt <= createdAt" in timestamp_validation,
        "command creation timestamps can bypass the bounded execution lease",
    )
    require("oni.command.v1" not in source, "legacy command V1 is still accepted")
    require("AccessTools" not in source and "System.Reflection" not in source, "executor still uses reflection for game actions")
    require("SimMessages.Dig" not in source and "WorldDamage" not in source, "executor contains instant-dig APIs")
    require("def.Build(" not in source, "executor contains direct completed-building creation")
    require(
        "gameObject?.GetComponent<Constructable>()" not in probe_source
        and "target.Blueprint?.GetComponent<KPrefabID>()" not in probe_source
        and "blueprint?.GetComponent<Constructable>()" not in source,
        "blueprint cancellation can dereference a destroyed Unity object",
    )
    require(
        "CandidateScanCellBudget" in probe_source
        and "DigCandidateScanBudgetMs" in probe_source
        and "BlueprintCandidateScanBudgetMs" in probe_source
        and "budget.Elapsed.TotalMilliseconds" in probe_source,
        "candidate refreshes are missing bounded per-snapshot work budgets",
    )
    require(
        "for (int radius = 0; radius <= maxRadius" not in probe_source,
        "candidate refresh still performs an unbounded concentric world scan",
    )
    require(
        "ConcurrentQueue<IDictionary<string, object>>" in writer_source
        and "PendingRecords.Enqueue(record)" in writer_source
        and "PrepareRecordForWrite(record)" in writer_source
        and "TryValidateJsonValue(record" in writer_source
        and "JsonLineWriter.Write(line, recordToWrite)" in writer_source,
        "snapshot preflight/serialization is not delegated to the writer thread",
    )
    require(
        "ConcurrentQueue<string>" not in writer_source
        and "StringBuilder builder" not in writer_source
        and "JsonLineWriter.Serialize(record)" not in writer_source,
        "snapshot writer still allocates full JSON lines on the game thread",
    )
    require(
        "private const int DigDesignationLimit = 32;" in probe_source,
        "untracked dig designation sampling exceeds the command target limit",
    )
    require(
        "postfix: new HarmonyMethod" in lifecycle_source
        and "nameof(OnSaveFinished)" in lifecycle_source
        and 'ProbeWriter.WriteEvent("save_write_completed"' in lifecycle_source
        and '"file_rewrite_observed"' in lifecycle_source,
        "save lifecycle has no auditable completion postfix",
    )
    require(
        "CompleteSaveWrite" in session_source
        and "saveIdHash = activeHash" in session_source
        and "saveDirty = false" in session_source
        and "SaveGame.GetFileInfo(path) != null" in session_source
        and 'failureReason = "save_file_unreadable"' in session_source
        and 'failureReason = "file_rewrite_not_observed"' in session_source,
        "save completion does not rotate identity and fail closed",
    )
    require(
        'SCHEMA = "oni.test-save-lineage.v1"' in lineage_source
        and "save_absent_at_reservation" in lineage_source
        and "previous_checkpoint_hash" in lineage_source
        and "save_write_completed" in lineage_source
        and "disabled_policy_evidence" in lineage_source
        and "checkpoint_hash(record)" in lineage_source,
        "one-save checkpoint lineage contract is missing",
    )
    return {
        "required_fragments": len(required_fragments),
        "future_created_at_rejected": True,
        "single_mutation_inflight": True,
        "verification_identity_policy_continuity": True,
        "unknown_outcome_pauses_and_latches": True,
        "legacy_v1_absent": True,
        "reflection_absent": True,
        "instant_dig_absent": True,
        "direct_build_absent": True,
        "destroyed_unity_object_guards_present": True,
        "candidate_refresh_bounded": True,
        "snapshot_serialization_streamed": True,
        "dig_designation_sample_bounded": True,
        "save_completion_fail_closed": True,
        "save_lineage_hash_chained": True,
        "durable_execution_claim_before_dispatch": True,
        "async_ack_durability_honest": True,
        "startup_executing_recovery_latched": True,
        "prepared_confirming_waits_for_journal_recovery": True,
        "non_mutating_direct_done": True,
        "python_executing_directory_present": True,
        "approving_claim_startup_non_mutating": True,
        "executor_polls_only_approved": True,
        "shared_strict_json_parser": True,
    }


def run_case(name: str, function: Callable[[pathlib.Path], Dict[str, Any]], root: pathlib.Path) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        details = function(root / name)
        return {
            "name": name,
            "status": "passed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "details": details,
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "classification": "behavior_fail",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }


def main() -> int:
    cases: List[tuple[str, Callable[[pathlib.Path], Dict[str, Any]]]] = [
        ("policy_fail_closed", case_policy_fail_closed),
        ("approval_gates", case_approval_gates),
        ("dedupe_envelope", case_dedupe_and_envelope),
        ("dynamic_condition_contracts", case_dynamic_condition_contracts),
        ("state_hash_decision_surface", case_state_hash_decision_surface),
        ("atomic_command_write", case_atomic_command_write),
        ("suggestion_gate_preserves_operator_commands", case_suggestion_gate_preserves_operator_commands),
        ("executor_static_contract", case_executor_static_contract),
    ]
    with tempfile.TemporaryDirectory(prefix="oni-command-v2-") as temporary:
        root = pathlib.Path(temporary)
        results = [run_case(name, function, root) for name, function in cases]

    passed = all(result["status"] == "passed" for result in results)
    report = {
        "schema": "oni.command-safety-report.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "cases": results,
    }
    report_path = PROJECT_ROOT / "reports" / "command-safety.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for result in results:
        detail = result.get("error") or json.dumps(result.get("details"), ensure_ascii=False, sort_keys=True)
        print(f"[{result['status'].upper()}] {result['name']}: {detail}")
    print(f"Report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
