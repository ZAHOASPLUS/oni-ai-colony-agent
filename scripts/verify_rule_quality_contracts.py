#!/usr/bin/env python3
from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
from typing import Any, Dict


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.colony_plan import _evaluate_condition
from agent.command_queue import ALLOWED_ACTIONS, COMMAND_SCHEMA, sync_pending_commands
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    DUPLICANTS_TOTAL_CONTRACT_ID,
    DUPLICANTS_TOTAL_SOURCE,
    GAME_PAUSED_CONTRACT_ID,
    REGISTERED_FOOD_CONTRACT_ID,
    RESEARCH_CURRENT_CONTRACT_ID,
    build_condition,
    condition_declaration_blockers,
    contract_for_path,
    contract_quality_fixture,
)
from agent.incidents import IncidentTracker
from agent.rules import AVAILABLE_ACTIONS, advice_from_incidents, evaluate_signals
from agent.command_queue import write_json_atomic
from scripts.verify_command_safety import write_authorizing_frame


FIXED_TS = "2026-08-30T12:00:00+00:00"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def quality(contract_id: str, status: str = "observed") -> Dict[str, Any]:
    return contract_quality_fixture(contract_id, status)


def state() -> Dict[str, Any]:
    return {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "session_id": "session-rule-contracts",
        "seq": 8,
        "snapshot_seq": 7,
        "snapshot_id": "session-rule-contracts:8",
        "ts": FIXED_TS,
        "game_build": "test",
        "mod_version": "test",
        "save_id_hash": "sha256:" + "a" * 64,
        "save_dirty": False,
        "world_id": 0,
        "sample": {"interval_seconds": 5.0},
        "writer": {
            "queue_depth": 0,
            "dropped_snapshot_count": 0,
            "fault_count": 0,
            "non_finite_numeric_count": 0,
        },
        "bridge": {
            "stale": False,
            "trust_ready": True,
            "trust_blockers": [],
            "replay_mode": False,
        },
        "game": {"paused": False},
        "duplicants": {"total": 3, "idle": 0, "stressed": 0},
        "resources": {"food_kcal": 3500.0},
        "alerts": [],
        "research": {"current": "FarmingTech"},
        "buildings": {
            "oxygen_diffuser": 1,
            "outhouse": 1,
            "manual_generator": 1,
            "captured_at": FIXED_TS,
        },
        "quality": {
            "game.paused": quality(GAME_PAUSED_CONTRACT_ID),
            "duplicants.total": quality(DUPLICANTS_TOTAL_CONTRACT_ID),
            "duplicants.idle": quality(DUPLICANTS_IDLE_CONTRACT_ID),
            "duplicants.stressed": quality(DUPLICANTS_STRESSED_CONTRACT_ID),
            "resources.food_kcal": quality(REGISTERED_FOOD_CONTRACT_ID),
            "alerts": quality(ALERTS_CANONICAL_CONTRACT_ID),
            "research.current": quality(RESEARCH_CURRENT_CONTRACT_ID),
            "buildings.oxygen_diffuser": quality(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
            "buildings.outhouse": quality(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
            "buildings.manual_generator": quality(BUILDING_COMPLETED_COUNT_CONTRACT_ID),
        },
    }


def signal(snapshot: Dict[str, Any], incident_id: str) -> Dict[str, Any]:
    return next(item for item in evaluate_signals(snapshot) if item["incident_id"] == incident_id)


def case_duplicant_total_contract_alignment() -> Dict[str, Any]:
    contract = contract_for_path("duplicants.total")
    require(contract is not None, "Python duplicants.total contract is missing")
    require(contract.contract_id == DUPLICANTS_TOTAL_CONTRACT_ID, "Python total contract id drifted")
    require(contract.sources == (DUPLICANTS_TOTAL_SOURCE,), "Python total source drifted")

    contract_path = ROOT / "tools" / "game_verify" / "contracts" / "oni_telemetry.v1.json"
    telemetry_contract = json.loads(contract_path.read_text(encoding="utf-8"))
    total_fields = [field for field in telemetry_contract["fields"] if field.get("path") == "duplicants.total"]
    require(len(total_fields) == 1, "telemetry contract must define duplicants.total exactly once")
    quality_contract = total_fields[0].get("quality_contract")
    require(isinstance(quality_contract, dict), "duplicants.total JSON quality contract is missing")
    expected_lists = {
        "statuses": contract.statuses,
        "semantics": contract.semantics,
        "units": contract.units,
        "scopes": contract.scopes,
        "completeness": contract.completeness,
        "sources": contract.sources,
        "contract_ids": (contract.contract_id,),
    }
    for key, expected in expected_lists.items():
        require(tuple(quality_contract.get(key, ())) == tuple(expected), f"JSON total {key} drifted")
    require(
        quality_contract.get("captured_at") in contract.captures,
        "JSON total capture binding drifted",
    )

    csharp_contract = (ROOT / "mod" / "Source" / "Contracts" / "CommandFieldContracts.cs").read_text(
        encoding="utf-8-sig"
    )
    require(
        f'internal const string ActiveWorldDuplicantCountId = "{contract.contract_id}";' in csharp_contract,
        "C# total contract id drifted",
    )
    require(
        "ActiveWorldDuplicantCountSource" in csharp_contract and
        f'"{DUPLICANTS_TOTAL_SOURCE}";' in csharp_contract,
        "C# total source drifted",
    )
    require(
        "ActiveWorldDuplicantCountId,\n            ActiveWorldDuplicantCountSource,\n            \"live_count\""
        in csharp_contract,
        "C# total contract constructor is not bound to its constants",
    )

    producer = (ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs").read_text(
        encoding="utf-8-sig"
    )
    require(
        '{ "_total_source", CommandFieldContracts.ActiveWorldDuplicantCountSource }' in producer,
        "producer total source is not bound to the C# contract",
    )
    require(
        "CommandFieldContracts.ActiveWorldDuplicantCountId" in producer,
        "producer total quality contract id is missing",
    )
    return {
        "contract_id": contract.contract_id,
        "source": DUPLICANTS_TOTAL_SOURCE,
        "layers": ["producer", "csharp", "python", "json"],
    }


def case_food_contract() -> Dict[str, Any]:
    snapshot = state()
    low = signal(snapshot, "low_food")
    require(low["state"] == "active", "valid registered-food risk did not activate")
    require("cycles" not in low["reason"].lower(), "registered food was presented as a runway")
    require(
        {item["quality_contract_id"] for item in low["command"]["preconditions"]}
        == {REGISTERED_FOOD_CONTRACT_ID, DUPLICANTS_TOTAL_CONTRACT_ID},
        "food command did not bind both exact contracts",
    )

    estimated = copy.deepcopy(snapshot)
    estimated["quality"]["resources.food_kcal"] = quality(REGISTERED_FOOD_CONTRACT_ID, "estimated")
    estimated_signal = signal(estimated, "low_food")
    require(estimated_signal["state"] == "unknown", "bounded lower bound asserted a low-food incident")
    require(estimated_signal["command"] is None, "bounded lower bound emitted a command candidate")
    estimated_text = (
        estimated_signal["title"]
        + " "
        + estimated_signal["reason"]
    ).lower()
    require("lower bound" in estimated_text and "total" in estimated_text and "unknown" in estimated_text,
            "bounded food advice did not disclose lower-bound/unknown-total semantics")
    require(
        "cannot establish" in estimated_text and "runway" in estimated_text,
        "bounded food signal did not explicitly reject a low-total/runway inference",
    )

    tampered_fields = (
        "contract_id",
        "semantic",
        "unit",
        "scope",
        "completeness",
        "source",
        "captured_at",
    )
    for field in tampered_fields:
        tampered = copy.deepcopy(snapshot)
        tampered["quality"]["resources.food_kcal"][field] = "wrong"
        require(signal(tampered, "low_food")["state"] == "unknown", f"food {field} tamper did not block")
        total_tampered = copy.deepcopy(snapshot)
        total_tampered["quality"]["duplicants.total"][field] = "wrong"
        require(
            signal(total_tampered, "low_food")["state"] == "unknown",
            f"duplicants.total {field} tamper did not block",
        )

    wrong_pair = copy.deepcopy(snapshot)
    wrong_pair["quality"]["resources.food_kcal"]["status"] = "estimated"
    require(
        signal(wrong_pair, "low_food")["state"] == "unknown",
        "estimated+complete status/completeness mismatch did not block",
    )
    wrong_type = copy.deepcopy(snapshot)
    wrong_type["resources"]["food_kcal"] = "3500"
    require(signal(wrong_type, "low_food")["state"] == "unknown", "numeric string food did not block")
    fractional_count = copy.deepcopy(snapshot)
    fractional_count["duplicants"]["total"] = 3.5
    require(signal(fractional_count, "low_food")["state"] == "unknown", "fractional dupes did not block")
    excessive_count = copy.deepcopy(snapshot)
    excessive_count["duplicants"]["total"] = 1001
    require(signal(excessive_count, "low_food")["state"] == "unknown", "excessive dupes did not block")
    return {
        "tamper_count_per_field": len(tampered_fields),
        "observed_diagnostic_only": True,
        "bounded_unknown": True,
        "total_limit_blocked": True,
    }


def case_alert_contract() -> Dict[str, Any]:
    snapshot = state()
    snapshot["alerts"] = ["low_oxygen"]
    low = signal(snapshot, "low_oxygen")
    require(low["state"] == "active", "canonical low_oxygen did not activate")
    require(len(low["command"]["preconditions"]) == 2, "pause command did not bind alert and paused state")
    require(
        {item["quality_contract_id"] for item in low["command"]["preconditions"]}
        == {ALERTS_CANONICAL_CONTRACT_ID, GAME_PAUSED_CONTRACT_ID},
        "pause command contract ids are incomplete",
    )
    known_condition = build_condition(
        "alerts",
        "contains",
        "low_oxygen",
        ALERTS_CANONICAL_CONTRACT_ID,
    )
    require(
        not condition_declaration_blockers(known_condition),
        "known canonical alert operand was rejected",
    )
    unknown_condition = copy.deepcopy(known_condition)
    unknown_condition["value"] = "unknown_hazard"
    require(
        "condition_value_item_unknown" in condition_declaration_blockers(unknown_condition),
        "format-valid unknown alert operand was accepted",
    )
    try:
        build_condition(
            "alerts",
            "contains",
            "unknown_hazard",
            ALERTS_CANONICAL_CONTRACT_ID,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("condition builder accepted a format-valid unknown alert operand")
    for non_alert in ("high_stress", "starvation", "sickness"):
        candidate = copy.deepcopy(snapshot)
        candidate["alerts"] = [non_alert]
        require(signal(candidate, "low_oxygen")["state"] == "inactive", f"{non_alert} matched by substring")
    unknown_alert = copy.deepcopy(snapshot)
    unknown_alert["alerts"] = ["unknown_hazard"]
    require(signal(unknown_alert, "low_oxygen")["state"] == "unknown", "unknown canonical alert ID did not block")
    malformed = copy.deepcopy(snapshot)
    malformed["alerts"] = "low_oxygen"
    require(signal(malformed, "low_oxygen")["state"] == "unknown", "string alert payload did not block")
    wrong_scope = copy.deepcopy(snapshot)
    wrong_scope["quality"]["alerts"]["scope"] = "retired_world"
    require(signal(wrong_scope, "low_oxygen")["state"] == "unknown", "wrong alert scope did not block")
    unsorted = copy.deepcopy(snapshot)
    unsorted["alerts"] = ["starvation", "low_oxygen"]
    require(signal(unsorted, "low_oxygen")["state"] == "unknown", "unsorted canonical alerts did not block")

    tracker = IncidentTracker()
    start = dt.datetime(2026, 8, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    for index in range(7):
        current = copy.deepcopy(snapshot)
        current["seq"] = index + 2
        current["snapshot_seq"] = index + 1
        current["snapshot_id"] = f"session-rule-contracts:{index + 2}"
        current["ts"] = (start + dt.timedelta(seconds=index * 5)).isoformat()
        tracker.update(current, evaluate_signals(current))
    opened = {item["incident_id"]: item for item in tracker.open_incidents()}
    require(opened.get("low_oxygen", {}).get("command", {}).get("action") == "set_paused", "window did not open")
    return {"exact_identifier": True, "unknown_operand_blocked": True, "window_opened": True}


def case_other_rule_types() -> Dict[str, Any]:
    snapshot = state()
    snapshot["research"]["current"] = ""
    research = signal(snapshot, "no_active_research")
    require(research["state"] == "active", "valid empty research selection did not activate")
    require(
        research["command"] is None,
        "empty research selection guessed an executable technology id",
    )
    require(
        "set_research" in ALLOWED_ACTIONS
        and any(item.get("action") == "set_research" for item in AVAILABLE_ACTIONS),
        "removing the guessed rule also removed explicit set_research capability",
    )
    invalid_research = copy.deepcopy(snapshot)
    invalid_research["research"]["current"] = None
    require(
        signal(invalid_research, "no_active_research")["state"] == "unknown",
        "observed null research was treated as inactive",
    )

    invalid_idle = copy.deepcopy(snapshot)
    invalid_idle["duplicants"]["idle"] = "none"
    require(signal(invalid_idle, "idle_duplicants")["state"] == "unknown", "invalid idle count failed open")

    building = copy.deepcopy(snapshot)
    building["buildings"]["oxygen_diffuser"] = 0
    require(
        signal(building, "missing_oxygen_diffuser")["state"] == "active",
        "valid completed-prefab diagnostic did not activate",
    )
    current_probe_shape = copy.deepcopy(building)
    current_probe_shape["quality"]["buildings.oxygen_diffuser"] = {
        "status": "estimated",
        "source": "typed_active_world",
    }
    require(
        signal(current_probe_shape, "missing_oxygen_diffuser")["state"] == "unknown",
        "current uncertified building estimate became executable fact",
    )
    missing_capture = copy.deepcopy(building)
    missing_capture["buildings"].pop("captured_at")
    require(
        signal(missing_capture, "missing_oxygen_diffuser")["state"] == "unknown",
        "building cache without capture timestamp did not block",
    )
    return {
        "strict_research": True,
        "research_observation_only": True,
        "explicit_set_research_preserved": True,
        "strict_counts": True,
    }


def case_plan_conditions_fail_closed() -> Dict[str, Any]:
    snapshot = state()
    rolling = {
        "path": "materials.active_world_cells.clean_oxygen_kg",
        "op": "gt",
        "value": 0.0,
        "quality": ["estimated"],
        "quality_contract_id": "oni.qc.fake.rolling.v1",
    }
    snapshot["materials"] = {"active_world_cells": {"clean_oxygen_kg": 100.0}}
    snapshot["quality"][rolling["path"]] = {
        "status": "estimated",
        "source": "spoof",
        "contract_id": "oni.qc.fake.rolling.v1",
        "semantic": "wrong",
        "unit": "wrong",
        "scope": "wrong",
        "completeness": "bounded",
        "captured_at": "snapshot.ts",
    }
    require(
        not _evaluate_condition(snapshot, rolling, action="set_research"),
        "unknown rolling contract authorized a mutation",
    )
    registered_food = build_condition(
        "resources.food_kcal",
        "gt",
        0.0,
        REGISTERED_FOOD_CONTRACT_ID,
    )
    require(
        not _evaluate_condition(snapshot, registered_food, action="set_research"),
        "risk-only registered food authorized a mutation",
    )
    research = build_condition(
        "research.current",
        "eq",
        "FarmingTech",
        RESEARCH_CURRENT_CONTRACT_ID,
    )
    require(_evaluate_condition(snapshot, research, action="set_research"), "valid research contract failed")
    return {"unknown_contract_blocked": True, "risk_contract_blocked": True}


def case_suggestion_sync() -> Dict[str, Any]:
    snapshot = state()
    snapshot["alerts"] = ["low_oxygen"]
    low = signal(snapshot, "low_oxygen")
    advice = {
        "incident_id": "low_oxygen",
        "title": low["title"],
        "risk": low["risk"],
        "reason": low["reason"],
        "evidence": low["evidence"],
        "source_snapshot_id": snapshot["snapshot_id"],
        "incident_opened_at": snapshot["ts"],
        "command": low["command"],
    }

    def prepare_bridge(path: pathlib.Path, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        write_json_atomic(
            path / "action-policy.json",
            {
                "schema": "oni.action-policy.v1",
                "actions_enabled": True,
                "suggestions_enabled": True,
                "allowed_actions": ["set_paused"],
                "allowed_save_hashes": [snapshot["save_id_hash"]],
                "command_ttl_seconds": 30,
                "max_pending_commands": 1,
            },
        )
        return write_authorizing_frame(path, snapshot)

    with tempfile.TemporaryDirectory(prefix="oni-rule-contract-sync-") as raw:
        bridge = pathlib.Path(raw)
        frame = prepare_bridge(bridge, snapshot)
        created = sync_pending_commands(bridge, snapshot, [advice], proposal_frame=frame)
        require(len(created) == 1, "trusted contract-valid suggestion was not created")
        require(created[0]["schema"] == COMMAND_SCHEMA == "oni.command.v3", "command v3 was not emitted")

    untrusted = copy.deepcopy(snapshot)
    untrusted["bridge"]["trust_ready"] = False
    untrusted["bridge"]["trust_blockers"] = ["sequence_gap"]
    with tempfile.TemporaryDirectory(prefix="oni-rule-contract-untrusted-") as raw:
        bridge = pathlib.Path(raw)
        frame = prepare_bridge(bridge, untrusted)
        require(
            not sync_pending_commands(bridge, untrusted, [advice], proposal_frame=frame),
            "untrusted bridge state wrote a suggestion",
        )

    mismatch = copy.deepcopy(advice)
    mismatch["source_snapshot_id"] = "session-rule-contracts:7"
    with tempfile.TemporaryDirectory(prefix="oni-rule-contract-mismatch-") as raw:
        bridge = pathlib.Path(raw)
        frame = prepare_bridge(bridge, snapshot)
        require(
            not sync_pending_commands(bridge, snapshot, [mismatch], proposal_frame=frame),
            "source snapshot mismatch wrote a suggestion",
        )

    bad_contract = copy.deepcopy(snapshot)
    bad_contract["quality"]["alerts"]["unit"] = "watts"
    with tempfile.TemporaryDirectory(prefix="oni-rule-contract-bad-quality-") as raw:
        bridge = pathlib.Path(raw)
        frame = prepare_bridge(bridge, bad_contract)
        require(
            not sync_pending_commands(bridge, bad_contract, [advice], proposal_frame=frame),
            "bad current quality metadata wrote a suggestion",
        )
    return {"trusted_created": 1, "untrusted_blocked": True, "mismatch_blocked": True}


def main() -> int:
    results = {
        "duplicant_total_alignment": case_duplicant_total_contract_alignment(),
        "food": case_food_contract(),
        "alerts": case_alert_contract(),
        "other_rules": case_other_rule_types(),
        "plan_conditions": case_plan_conditions_fail_closed(),
        "suggestion_sync": case_suggestion_sync(),
    }
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
