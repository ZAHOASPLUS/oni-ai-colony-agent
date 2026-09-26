#!/usr/bin/env python3
"""Pure-code regression checks for the fail-closed six-phase strategy."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Any, Callable, Dict, List, Mapping, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent.strategy as strategy_module
from agent.decision import _project_strategy
from agent.strategy import (
    CERTIFYING_FIELD_CONTRACTS,
    PHASE_EXIT_GATES,
    PHASE_STABILITY_MIN_GAME_CYCLES,
    PHASE_STABILITY_MIN_SECONDS,
    PHASE_STABILITY_SAMPLES,
    STRATEGY_SCHEMA,
    PhaseEvidenceTracker,
    StrategyError,
    collect_metrics,
    evaluate_strategy,
    validate_strategy,
)
from agent.field_contracts import (
    ALERTS_CANONICAL_CONTRACT_ID,
    DUPLICANTS_IDLE_CONTRACT_ID,
    DUPLICANTS_STRESSED_CONTRACT_ID,
    FUNCTIONAL_BUILDINGS_CONTRACT_ID,
    FUNCTIONAL_BUILDINGS_SCHEMA,
    contract_quality_fixture,
)


PROOF_HASH = "sha256:" + "b" * 64


def observed(path: str) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "status": "observed",
        "source": "strategy_fixture",
        "captured_at": "snapshot.ts",
    }
    contract = CERTIFYING_FIELD_CONTRACTS.get(path)
    if contract is not None:
        for entry_key, contract_key in (
            ("semantic", "semantics"),
            ("unit", "units"),
            ("scope", "scopes"),
            ("completeness", "completeness"),
            ("authority", "authorities"),
            ("source", "sources"),
            ("contract_id", "contract_ids"),
        ):
            allowed = contract.get(contract_key)
            if allowed:
                value[entry_key] = allowed[0]
        if "completeness" not in value:
            value["completeness"] = "complete"
        if contract.get("require_proof_hashes"):
            value.update(
                {
                    "input_hash": PROOF_HASH,
                    "policy_hash": PROOF_HASH,
                    "policy_version": "strategy-fixture.v1",
                }
            )
    return value


def trusted_fixture_proof(
    _state: Mapping[str, Any],
    _path: str,
    entry: Mapping[str, Any],
) -> bool:
    """Test-only in-process verifier; production never installs this hook."""

    return bool(
        entry.get("source") == "strategy_fixture"
        and entry.get("input_hash") == PROOF_HASH
        and entry.get("policy_hash") == PROOF_HASH
        and entry.get("policy_version") == "strategy-fixture.v1"
    )


def evaluate_fixture(state: Mapping[str, Any], **kwargs: Any) -> Dict[str, Any]:
    return evaluate_strategy(
        state,
        trusted_proof_verifier=trusted_fixture_proof,
        **kwargs,
    )


def base_state() -> Dict[str, Any]:
    session = "session-strategy-fixture"
    state: Dict[str, Any] = {
        "schema": "oni.state.v2",
        "event": "state_snapshot",
        "session_id": session,
        "seq": 42,
        "snapshot_seq": 42,
        "snapshot_id": f"{session}:42",
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "game_build": "cl-test",
        "mod_version": "0.7.0-test",
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
        "bridge": {"stale": False, "snapshot_age_seconds": 0.1, "trust_ready": True, "trust_blockers": []},
        "game": {"paused": True, "victory": True},
        "cycle": 200,
        "game_time_cycles": 200.5,
        "observation_scope": {
            "active_world_id": 0,
            "populated_world_ids": [0],
            "covered_world_ids": [0],
            "complete_for_populated_worlds": True,
        },
        "duplicants": {"total": 3, "stressed": 0, "idle": 0},
        "resources": {"food_kcal": 30_000.0, "oxygen_kg_estimate": 500.0, "water_kg_estimate": 3_000.0},
        "alerts": [],
        "research": {"current": "SpaceProgram", "complete": ["FarmingTech"]},
        "buildings": {"oxygen_diffuser": 1, "outhouse": 1, "manual_generator": 1},
        "production": {
            "food_kcal_per_cycle": 4_000.0,
            "oxygen_kg_per_cycle": 200.0,
            "water_kg_per_cycle": 1_000.0,
            "power_watts": 1_200.0,
        },
        # Synthetic but explicit industrial result evidence.  These are
        # completion observations, not building/blueprint counts; each field
        # is bound to the in-process fixture verifier below.
        "industry": {
            "production": {"completed": True},
            "delivery": {"completed": True},
            "inputs": {"supplied": True},
            "heat": {"removed": True},
            "waste": {"handled": True},
        },
        "consumption": {
            "oxygen_kg_per_cycle": 180.0,
            "water_kg_per_cycle": 800.0,
            "power_watts": 1_000.0,
        },
        "power": {
            "storage_mode": "bounded",
            "stored_joules": 800.0,
            "capacity_joules": 1_000.0,
            "battery_percent": 80.0,
            "critical_load_covered": True,
            "independent_source_count": 2,
        },
        "oxygen": {"independent_source_count": 2},
        "water": {"independent_source_count": 2},
        "temperature": {"critical_zones_safe": True, "min_c": 18.0, "max_c": 28.0},
        "rockets": {"total": 1},
        "space": {
            "mission_ready": True,
            "mission_completed": True,
            "return_ready": True,
            "base_reserve_safe": True,
        },
        "resilience": {"n_minus_one_safe": True},
        "colony": {"sustainable": True},
        "quality": {},
    }
    paths = (
        "game.paused",
        "game.victory",
        "cycle",
        "game_time_cycles",
        "observation_scope.active_world_id",
        "observation_scope.populated_world_ids",
        "observation_scope.covered_world_ids",
        "observation_scope.complete_for_populated_worlds",
        "duplicants.total",
        "duplicants.stressed",
        "duplicants.idle",
        "resources.food_kcal",
        "resources.oxygen_kg_estimate",
        "resources.water_kg_estimate",
        "alerts",
        "research.current",
        "research.complete",
        "buildings.oxygen_diffuser",
        "buildings.outhouse",
        "buildings.manual_generator",
        "production.food_kcal_per_cycle",
        "production.oxygen_kg_per_cycle",
        "production.water_kg_per_cycle",
        "production.power_watts",
        "industry.production.completed",
        "industry.delivery.completed",
        "industry.inputs.supplied",
        "industry.heat.removed",
        "industry.waste.handled",
        "consumption.oxygen_kg_per_cycle",
        "consumption.water_kg_per_cycle",
        "consumption.power_watts",
        "power.storage_mode",
        "power.stored_joules",
        "power.capacity_joules",
        "power.battery_percent",
        "power.critical_load_covered",
        "power.independent_source_count",
        "oxygen.independent_source_count",
        "water.independent_source_count",
        "temperature.critical_zones_safe",
        "temperature.min_c",
        "temperature.max_c",
        "rockets.total",
        "space.mission_ready",
        "space.mission_completed",
        "space.return_ready",
        "space.base_reserve_safe",
        "resilience.n_minus_one_safe",
        "colony.sustainable",
    )
    state["quality"] = {path: observed(path) for path in paths}
    return state


def add_functional_outhouses(
    state: Dict[str, Any],
    operational_values: Sequence[Any],
    *,
    complete: bool = True,
    functional_world_id: int = 0,
    captured_at: str | None = None,
    include_quality: bool = True,
) -> Dict[str, Any]:
    state["functional_buildings"] = {
        "schema": FUNCTIONAL_BUILDINGS_SCHEMA,
        "captured_at": captured_at or state["ts"],
        "world_id": functional_world_id,
        "complete": complete,
        "instances": [
            {
                "instance_id": 100 + index,
                "world_id": functional_world_id,
                "prefab_id": "Outhouse",
                "operational": value,
            }
            for index, value in enumerate(operational_values)
        ],
    }
    if include_quality:
        state["quality"]["functional_buildings"] = contract_quality_fixture(
            FUNCTIONAL_BUILDINGS_CONTRACT_ID
        )
    return state


def add_functional_bootstrap_buildings(
    state: Dict[str, Any],
    *,
    diffuser: Sequence[Any] = (),
    generator: Sequence[Any] = (),
    captured_at: str | None = None,
) -> Dict[str, Any]:
    """Attach a strict functional-building fixture for oxygen and power gates."""

    instances = []
    next_id = 200
    for prefab_id, values in (("OxygenDiffuser", diffuser), ("ManualGenerator", generator)):
        for value in values:
            instances.append(
                {
                    "instance_id": next_id,
                    "world_id": 0,
                    "prefab_id": prefab_id,
                    "operational": value,
                }
            )
            next_id += 1
    state["functional_buildings"] = {
        "schema": FUNCTIONAL_BUILDINGS_SCHEMA,
        "captured_at": captured_at or state["ts"],
        "world_id": 0,
        "complete": True,
        "instances": instances,
    }
    state["quality"]["functional_buildings"] = contract_quality_fixture(
        FUNCTIONAL_BUILDINGS_CONTRACT_ID
    )
    return state


def objective(strategy: Dict[str, Any], identifier: str) -> Dict[str, Any]:
    return next(item for item in strategy["objectives"] if item["id"] == identifier)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def case_happy_single_snapshot_is_not_certification() -> Dict[str, Any]:
    default_result = evaluate_strategy(base_state())
    require(
        default_result["phase"]["detected_from_single_snapshot"] == "bootstrap",
        "self-declared proof metadata was trusted without an in-process verifier",
    )
    result = evaluate_fixture(base_state())
    require(result["schema"] == STRATEGY_SCHEMA, "strategy schema was not upgraded")
    require(result["phase"]["id"] == "bootstrap", "one snapshot advanced the certified phase")
    require(result["phase"]["detected_from_single_snapshot"] == "sustain", "complete fixture did not detect all gates")
    require(result["readiness"]["status"] == "not_ready", "one snapshot was marked promotion-ready")
    require("stable_window_unverified" in result["readiness"]["blockers"], "stable-window blocker is missing")
    require(result["status"] == "ok", "complete observed safety evidence was not recognized")
    return {
        "schema": result["schema"],
        "detected": result["phase"]["detected_from_single_snapshot"],
        "default_proof": "blocked",
    }


def case_critical_domains_block() -> Dict[str, Any]:
    mutations: Dict[str, tuple[Callable[[Dict[str, Any]], None], str]] = {
        "oxygen_zero": (lambda state: state["resources"].__setitem__("oxygen_kg_estimate", 0.0), "oxygen_security"),
        "water_zero": (lambda state: state["resources"].__setitem__("water_kg_estimate", 0.0), "water_resilience"),
        "battery_zero": (
            lambda state: state["power"].update({"stored_joules": 0.0, "battery_percent": 0.0}),
            "power_resilience",
        ),
        "temperature_hot": (lambda state: state["temperature"].update({"critical_zones_safe": False, "max_c": 100.0}), "thermal_safety"),
        "temperature_cold": (lambda state: state["temperature"].update({"critical_zones_safe": False, "min_c": -100.0}), "thermal_safety"),
    }
    statuses: Dict[str, str] = {}
    for name, (mutate, objective_id) in mutations.items():
        state = base_state()
        mutate(state)
        result = evaluate_fixture(state)
        statuses[name] = objective(result, objective_id)["status"]
        require(statuses[name] == "at_risk", f"{name} did not become at_risk")
        require(result["status"] == "at_risk", f"{name} left top-level status ok")
        require(result["phase"]["detected_from_single_snapshot"] not in {"space", "sustain"}, f"{name} bypassed earlier gates")
    return statuses


def case_strategy_cannot_issue_pause() -> Dict[str, Any]:
    state = base_state()
    state["alerts"] = ["suffocation"]
    state["resources"]["oxygen_kg_estimate"] = 0.0
    result = evaluate_fixture(state)
    recommendations = [
        action
        for objective_item in result["objectives"]
        for action in objective_item.get("recommended_actions") or []
    ]
    checkable = recommendations + list(result.get("next_actions") or [])
    require(checkable, "strategy fixture produced no recommendations to inspect")
    require(
        all(action.get("action") != "set_paused" for action in checkable),
        "strategy bypassed the survival emergency arbiter",
    )

    objective_tamper = copy.deepcopy(result)
    objective_tamper["objectives"][0]["recommended_actions"].append(
        {
            "action": "set_paused",
            "requires_approval": True,
            "auto_execute": False,
        }
    )
    try:
        validate_strategy(objective_tamper)
    except StrategyError as exc:
        objective_error = str(exc)
    else:
        raise AssertionError("objective pause tamper passed strategy validation")

    next_action_tamper = copy.deepcopy(result)
    next_action_tamper["next_actions"] = [
        {
            "action": "set_paused",
            "requires_approval": True,
            "auto_execute": False,
        }
    ]
    try:
        validate_strategy(next_action_tamper)
    except StrategyError as exc:
        next_action_error = str(exc)
    else:
        raise AssertionError("next-action pause tamper passed strategy validation")

    return {
        "recommendations_checked": len(checkable),
        "objective_tamper": objective_error,
        "next_action_tamper": next_action_error,
    }


def case_research_action_requires_adapter_selection() -> Dict[str, Any]:
    state = base_state()
    state["research"] = {"current": False, "complete": ["FarmingTech"]}
    result = evaluate_fixture(state)
    item = objective(result, "research_progress")
    actions = item["recommended_actions"]
    require(len(actions) == 1, "research risk did not produce one bounded action")
    action = actions[0]
    require(action["action"] == "set_research", "research risk selected the wrong action")
    require(action["args"] == {"selection": "adapter_required", "technology_id": None}, "research action invented a technology id")
    require(action["requires_approval"] is True and action["auto_execute"] is False, "research action bypassed approval")
    return {"action": action["action"], "selection": action["args"]["selection"]}


def case_rate_balance_and_markers_fail_closed() -> Dict[str, Any]:
    oxygen = base_state()
    oxygen["production"]["oxygen_kg_per_cycle"] = 0.0
    oxygen_result = evaluate_fixture(oxygen)
    require(objective(oxygen_result, "renewable_oxygen")["status"] == "at_risk", "zero oxygen production was certified")

    food = base_state()
    food["duplicants"]["total"] = 3.0
    food_result = evaluate_fixture(food)
    require(objective(food_result, "renewable_food")["status"] == "satisfied", "integer-valued numeric population was not accepted")

    food = base_state()
    food["production"]["food_kcal_per_cycle"] = 500.0
    food_result = evaluate_fixture(food)
    require(objective(food_result, "renewable_food")["status"] == "at_risk", "food production below demand was certified")
    require("2500.0 kcal/cycle" in objective(food_result, "renewable_food")["reason"], "food deficit was not quantified")

    bypass = base_state()
    bypass["resources"]["water_kg_estimate"] = 0.0
    bypass["space"]["mission_completed"] = True
    bypass["colony"]["sustainable"] = True
    bypass_result = evaluate_fixture(bypass)
    require(bypass_result["phase"]["detected_from_single_snapshot"] == "stabilize", "space/sustainable marker bypassed water gate")
    require(objective(bypass_result, "indefinite_survival")["status"] != "satisfied", "marker alone certified sustainability")
    return {"oxygen": "at_risk", "food": "at_risk", "bypass_detected": "stabilize"}


def case_estimated_cannot_certify() -> Dict[str, Any]:
    state = base_state()
    state["resources"]["water_kg_estimate"] = 0.0
    state["quality"]["resources.water_kg_estimate"] = {"status": "estimated", "source": "fixture_estimate"}
    result = evaluate_fixture(state)
    require(
        objective(result, "water_resilience")["status"] == "unknown",
        "metadata-free estimate entered the safety gate",
    )
    require(result["phase"]["detected_from_single_snapshot"] == "stabilize", "estimated value advanced the phase")

    unsupported = base_state()
    unsupported["quality"]["buildings.outhouse"] = {"status": "unsupported", "source": "none"}
    unsupported_result = evaluate_fixture(unsupported)
    require(objective(unsupported_result, "sanitation")["status"] == "unknown", "unsupported diagnostic value was consumed")
    return {"water": "unknown", "sanitation": "unknown"}


def add_visible_cell_aggregate(
    state: Dict[str, Any],
    *,
    clean_oxygen_kg: float,
    polluted_oxygen_kg: float,
    captured_at: str = "materials.active_world_cells.aggregate_window_completed_at",
) -> None:
    state["materials"] = {
        "active_world_cells": {
            "clean_oxygen_kg": clean_oxygen_kg,
            "polluted_oxygen_kg": polluted_oxygen_kg,
            "aggregate_window_completed_at": state["ts"],
        }
    }
    for path in (
        "materials.active_world_cells.clean_oxygen_kg",
        "materials.active_world_cells.polluted_oxygen_kg",
    ):
        state["quality"][path] = {
            "status": "estimated",
            "source": "rolling_cell_scan_fixture",
            "semantic": "rolling_window_gauge",
            "unit": "kg",
            "scope": "active_world_visible_cells",
            "completeness": "complete",
            "captured_at": captured_at,
        }


def case_visible_cell_aggregate_is_risk_only() -> Dict[str, Any]:
    empty = base_state()
    add_visible_cell_aggregate(empty, clean_oxygen_kg=0.0, polluted_oxygen_kg=0.0)
    empty_result = evaluate_fixture(empty)
    require(
        objective(empty_result, "oxygen_security")["status"] == "at_risk",
        "zero complete visible-cell oxygen aggregate did not raise conservative risk",
    )

    positive_only = base_state()
    positive_only["resources"]["oxygen_kg_estimate"] = None
    positive_only["quality"]["resources.oxygen_kg_estimate"] = {"status": "unsupported", "source": "none"}
    add_visible_cell_aggregate(positive_only, clean_oxygen_kg=10.0, polluted_oxygen_kg=2.0)
    positive_result = evaluate_fixture(positive_only)
    require(
        objective(positive_result, "oxygen_security")["status"] == "unknown",
        "positive rolling cell aggregate certified a complete oxygen reserve",
    )

    bad_binding = base_state()
    add_visible_cell_aggregate(
        bad_binding,
        clean_oxygen_kg=0.0,
        polluted_oxygen_kg=0.0,
        captured_at="snapshot.ts",
    )
    bad_binding_result = evaluate_fixture(bad_binding)
    require(
        objective(bad_binding_result, "oxygen_security")["status"] == "satisfied",
        "wrong captured_at binding entered the rolling-cell risk path",
    )

    missing_target = base_state()
    add_visible_cell_aggregate(missing_target, clean_oxygen_kg=0.0, polluted_oxygen_kg=0.0)
    missing_target["materials"]["active_world_cells"].pop("aggregate_window_completed_at")
    missing_target_result = evaluate_fixture(missing_target)
    require(
        objective(missing_target_result, "oxygen_security")["status"] == "satisfied",
        "missing captured_at target entered the rolling-cell risk path",
    )

    stale_capture = base_state()
    add_visible_cell_aggregate(stale_capture, clean_oxygen_kg=0.0, polluted_oxygen_kg=0.0)
    snapshot_time = dt.datetime.fromisoformat(stale_capture["ts"])
    stale_capture["materials"]["active_world_cells"]["aggregate_window_completed_at"] = (
        snapshot_time - dt.timedelta(seconds=901)
    ).isoformat()
    stale_capture_result = evaluate_fixture(stale_capture)
    require(
        objective(stale_capture_result, "oxygen_security")["status"] == "satisfied",
        "over-age rolling aggregate entered the oxygen risk path",
    )
    return {
        "zero": "at_risk",
        "positive_only": "unknown",
        "wrong_capture": "ignored",
        "missing_capture_target": "ignored",
        "over_age": "ignored",
    }


def set_registered_food_quality(
    state: Dict[str, Any],
    *,
    status: str = "observed",
    completeness: str = "complete",
    scope: str = "active_world_registered_non_private_edibles",
) -> None:
    state["quality"]["resources.food_kcal"] = {
        "status": status,
        "source": "ClusterManager.activeWorld.worldInventory.GetPickupables(GameTags.Edible)+!GameTags.StoredPrivate",
        "semantic": "registered_non_private_inventory_sum",
        "unit": "kcal",
        "scope": scope,
        "completeness": completeness,
        "captured_at": "snapshot.ts",
        "contract_id": "oni.qc.resources.registered_food.v1",
    }


def case_registered_food_is_risk_only() -> Dict[str, Any]:
    low = base_state()
    low["resources"]["food_kcal"] = 3_500.0
    set_registered_food_quality(low)
    low_result = evaluate_fixture(low)
    require(
        objective(low_result, "food_reserve")["status"] == "unknown",
        "registered non-private inventory asserted an accessible-food risk",
    )
    require(
        low_result["metrics"]["food_runway_cycles"] is None,
        "registered non-private inventory was converted into a food runway",
    )
    require(
        "runway remain unproven" in objective(low_result, "food_reserve")["reason"],
        "observed registered-food diagnostic did not preserve the accessibility caveat",
    )

    high = base_state()
    set_registered_food_quality(high)
    high_result = evaluate_fixture(high)
    require(
        objective(high_result, "food_reserve")["status"] == "unknown",
        "high registered food was treated as reachable/permissioned reserve",
    )

    bounded = base_state()
    bounded["resources"]["food_kcal"] = 3_500.0
    set_registered_food_quality(bounded, status="estimated", completeness="bounded")
    bounded_result = evaluate_fixture(bounded)
    require(
        objective(bounded_result, "food_reserve")["status"] == "unknown",
        "bounded lower bound asserted a low-food risk",
    )
    require(
        bounded_result["metrics"]["food_runway_cycles"] is None,
        "bounded registered-food lower bound was converted into a runway",
    )
    require(
        "lower bound" in objective(bounded_result, "food_reserve")["reason"]
        and "remain unknown" in objective(bounded_result, "food_reserve")["reason"],
        "bounded registered-food diagnostic lost its lower-bound/unknown semantics",
    )

    invalid_pair = base_state()
    invalid_pair["resources"]["food_kcal"] = 3_500.0
    set_registered_food_quality(invalid_pair, status="observed", completeness="bounded")
    invalid_pair_result = evaluate_fixture(invalid_pair)
    require(
        objective(invalid_pair_result, "food_reserve")["status"] == "unknown",
        "inconsistent registered-food status/completeness entered the risk path",
    )

    wrong_scope = base_state()
    wrong_scope["resources"]["food_kcal"] = 3_500.0
    set_registered_food_quality(wrong_scope, scope="active_world_accessible_edibles")
    wrong_scope_result = evaluate_fixture(wrong_scope)
    require(
        objective(wrong_scope_result, "food_reserve")["status"] == "unknown",
        "mismatched registered-food contract entered the risk path",
    )

    wrong_source = base_state()
    wrong_source["resources"]["food_kcal"] = 3_500.0
    set_registered_food_quality(wrong_source)
    wrong_source["quality"]["resources.food_kcal"]["source"] = "attacker.self_declared"
    wrong_source_result = evaluate_fixture(wrong_source)
    require(
        objective(wrong_source_result, "food_reserve")["status"] == "unknown",
        "mismatched registered-food source entered the risk path",
    )

    wrong_contract = base_state()
    wrong_contract["resources"]["food_kcal"] = 3_500.0
    set_registered_food_quality(wrong_contract)
    wrong_contract["quality"]["resources.food_kcal"]["contract_id"] = "oni.qc.attacker.v1"
    wrong_contract_result = evaluate_fixture(wrong_contract)
    require(
        objective(wrong_contract_result, "food_reserve")["status"] == "unknown",
        "mismatched registered-food contract id entered the risk path",
    )
    return {
        "low": "unknown_no_runway",
        "high": "unknown",
        "bounded": "unknown_no_runway",
        "invalid_status_pair": "ignored",
        "wrong_scope": "ignored",
        "wrong_source": "ignored",
        "wrong_contract_id": "ignored",
    }


def case_trust_and_cycle_fail_closed() -> Dict[str, Any]:
    stale = base_state()
    stale["bridge"] = {"stale": True, "trust_ready": False, "trust_blockers": ["snapshot_stale"]}
    stale_result = evaluate_fixture(stale)
    require(stale_result["status"] == "blocked", "stale state did not block strategy")
    require(stale_result["operating_mode"] == "recover", "stale state did not enter recovery")

    contradictory = base_state()
    contradictory["bridge"] = {
        "stale": False,
        "trust_ready": True,
        "trust_blockers": ["sequence_gap"],
    }
    contradictory_result = evaluate_fixture(contradictory)
    require(contradictory_result["status"] == "blocked", "trust_ready bypassed a declared blocker")

    malformed = base_state()
    malformed["bridge"]["trust_blockers"] = "none"
    malformed_result = evaluate_fixture(malformed)
    require(malformed_result["status"] == "blocked", "malformed trust blockers were accepted")

    world_mismatch = base_state()
    world_mismatch["world_id"] = 1
    world_mismatch_result = evaluate_fixture(world_mismatch)
    require(world_mismatch_result["status"] == "blocked", "cross-world snapshot was accepted")
    require(
        "active_world_identity_mismatch" in world_mismatch_result["telemetry"]["trust_blockers"],
        "cross-world blocker was not reported",
    )

    early = base_state()
    early["cycle"] = 1
    late = base_state()
    late["cycle"] = 10_000
    require(
        evaluate_fixture(early)["phase"]["detected_from_single_snapshot"]
        == evaluate_fixture(late)["phase"]["detected_from_single_snapshot"],
        "cycle number alone changed phase maturity",
    )
    return {
        "stale": "blocked",
        "contradictory": "blocked",
        "malformed_blockers": "blocked",
        "active_world_identity_mismatch": "blocked",
        "cycle_independent": True,
    }


def case_contract_scope_and_marker_spoofs_fail_closed() -> Dict[str, Any]:
    missing_bridge = base_state()
    missing_bridge.pop("bridge")
    missing_bridge_result = evaluate_fixture(missing_bridge)
    require(missing_bridge_result["status"] == "blocked", "missing bridge trust was accepted")

    incomplete_scope = base_state()
    incomplete_scope["observation_scope"].update(
        {
            "populated_world_ids": [0, 1],
            "covered_world_ids": [0],
            "complete_for_populated_worlds": False,
        }
    )
    # With alerts that cover every world a second populated world keeps
    # trust (review M8); with active-world-only alerts it cannot.
    all_world_result = evaluate_fixture(copy.deepcopy(incomplete_scope))
    require(all_world_result["status"] != "blocked", "all-world alerts did not keep multi-world trust")
    incomplete_scope["quality"]["alerts"].update(
        {
            "scope": "active_world",
            "source": "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+typed_monitor_predicates;catalog=oni.alert.catalog.active_world.v1",
        }
    )
    incomplete_result = evaluate_fixture(incomplete_scope)
    require(incomplete_result["status"] == "blocked", "incomplete populated-world coverage was accepted")
    require(
        "populated_world_not_covered" in incomplete_result["telemetry"]["trust_blockers"],
        "missing-world blocker was not reported",
    )

    forged_complete_scope = base_state()
    forged_complete_scope["observation_scope"].update(
        {
            "populated_world_ids": [1],
            "covered_world_ids": [0, 1],
            "complete_for_populated_worlds": True,
        }
    )
    forged_complete_result = evaluate_fixture(forged_complete_scope)
    require(
        forged_complete_result["status"] == "blocked",
        "forged multi-world complete scope was accepted",
    )
    forged_blockers = forged_complete_result["telemetry"]["trust_blockers"]
    require(
        "covered_world_ids_not_active_world_only" in forged_blockers,
        "forged covered-world expansion was not reported",
    )
    require(
        "complete_populated_world_ids_not_active_world_only" in forged_blockers,
        "forged complete populated-world identity was not reported",
    )

    missing_scope_quality = base_state()
    missing_scope_quality["quality"].pop("observation_scope.covered_world_ids")
    missing_scope_quality_result = evaluate_fixture(missing_scope_quality)
    require(
        missing_scope_quality_result["status"] == "blocked",
        "world coverage without per-field provenance was accepted",
    )

    wrong_unit = base_state()
    wrong_unit["quality"]["production.oxygen_kg_per_cycle"]["unit"] = "g/cycle"
    wrong_unit_result = evaluate_fixture(wrong_unit)
    require(
        objective(wrong_unit_result, "renewable_oxygen")["status"] == "unknown",
        "wrong-unit rate certified renewable oxygen",
    )

    numeric_string = base_state()
    numeric_string["production"]["oxygen_kg_per_cycle"] = "200.0"
    numeric_string_result = evaluate_fixture(numeric_string)
    require(
        objective(numeric_string_result, "renewable_oxygen")["status"] == "unknown",
        "numeric string certified renewable oxygen",
    )

    bad_proof_hash = base_state()
    bad_proof_hash["quality"]["production.oxygen_kg_per_cycle"]["input_hash"] = "sha256:not-a-hash"
    bad_hash_result = evaluate_fixture(bad_proof_hash)
    require(
        objective(bad_hash_result, "renewable_oxygen")["status"] == "unknown",
        "tampered evidence input hash certified renewable oxygen",
    )

    nonfinite = base_state()
    nonfinite["power"]["battery_percent"] = float("nan")
    nonfinite_result = evaluate_fixture(nonfinite)
    require(
        objective(nonfinite_result, "power_resilience")["status"] == "unknown",
        "NaN battery value entered a safety decision",
    )
    infinite = base_state()
    infinite["power"]["battery_percent"] = float("inf")
    infinite_result = evaluate_fixture(infinite)
    require(
        objective(infinite_result, "power_resilience")["status"] == "unknown",
        "infinite battery value entered a safety decision",
    )
    huge_integer = base_state()
    huge_integer["power"]["stored_joules"] = 10 ** 309
    huge_integer_result = evaluate_fixture(huge_integer)
    require(
        objective(huge_integer_result, "power_resilience")["status"] == "unknown",
        "oversized JSON integer entered a power safety decision",
    )
    require(
        collect_metrics(huge_integer)["qualities"]["battery"]
        == "contract_mismatch",
        "oversized JSON integer was not exposed as a power contract mismatch",
    )
    huge_idle = base_state()
    huge_idle["duplicants"]["idle"] = 10 ** 309
    huge_idle_metrics = collect_metrics(huge_idle)
    require(
        huge_idle_metrics["qualities"]["idle"] == "contract_mismatch",
        "oversized duplicant count was not rejected by its field contract",
    )
    huge_food = base_state()
    huge_food["resources"]["food_kcal"] = 10 ** 309
    huge_food_metrics = collect_metrics(huge_food)
    require(
        huge_food_metrics["qualities"]["registered_food"] == "contract_mismatch",
        "oversized food gauge was not rejected by its field contract",
    )
    for invalid_duration in (True, 10 ** 309):
        invalid_strategy = evaluate_fixture(base_state())
        invalid_strategy["readiness"]["required_duration_seconds"] = invalid_duration
        try:
            validate_strategy(invalid_strategy)
        except StrategyError:
            pass
        else:
            raise AssertionError(
                "strategy output validation accepted a boolean or oversized duration"
            )

    negative_count = base_state()
    negative_count["oxygen"]["independent_source_count"] = -1
    negative_count_result = evaluate_fixture(negative_count)
    require(
        objective(negative_count_result, "fault_tolerance")["status"] == "unknown",
        "negative topology count entered N-1 certification",
    )

    fractional_count = base_state()
    fractional_count["oxygen"]["independent_source_count"] = 2.5
    fractional_count_result = evaluate_fixture(fractional_count)
    require(
        objective(fractional_count_result, "fault_tolerance")["status"] == "unknown",
        "fractional topology count entered N-1 certification",
    )

    wrong_certified_scope = base_state()
    wrong_certified_scope["quality"]["space.return_ready"]["scope"] = "all_populated_worlds"
    wrong_scope_result = evaluate_fixture(wrong_certified_scope)
    require(
        objective(wrong_scope_result, "space_progress")["status"] != "satisfied",
        "cluster return proof accepted an all-populated-worlds scope",
    )

    marker_spoof = base_state()
    for path in (
        "power.critical_load_covered",
        "temperature.critical_zones_safe",
        "space.return_ready",
        "space.base_reserve_safe",
        "resilience.n_minus_one_safe",
        "colony.sustainable",
    ):
        marker_spoof["quality"][path] = {
            "status": "observed",
            "source": "unversioned_marker_spoof",
        }
    marker_result = evaluate_fixture(marker_spoof)
    require(
        marker_result["phase"]["detected_from_single_snapshot"] != "sustain",
        "unversioned conclusion markers bypassed evidence contracts",
    )
    require(
        objective(marker_result, "indefinite_survival")["status"] != "satisfied",
        "unversioned sustainable marker was certified",
    )
    return {
        "missing_bridge": "blocked",
        "incomplete_scope": "blocked",
        "missing_scope_quality": "blocked",
        "wrong_unit": "unknown",
        "numeric_string": "unknown",
        "bad_proof_hash": "unknown",
        "nan": "unknown",
        "infinity": "unknown",
        "negative_count": "unknown",
        "fractional_count": "unknown",
        "wrong_certified_scope": "unknown",
        "marker_spoof": "rejected",
    }


def case_duration_gap_and_game_time_fail_closed() -> Dict[str, Any]:
    start = dt.datetime.now(dt.timezone.utc)

    short_tracker = PhaseEvidenceTracker()
    short_result: Dict[str, Any] = {}
    for index in range(1, PHASE_STABILITY_SAMPLES["bootstrap"] + 1):
        state = base_state()
        state["seq"] = index
        state["snapshot_seq"] = index
        state["snapshot_id"] = f"{state['session_id']}:{state['seq']}"
        state["ts"] = (start + dt.timedelta(seconds=index)).isoformat(timespec="seconds")
        state["game_time_cycles"] = 200.0 + index
        short_result = short_tracker.update(evaluate_fixture(state))
    require(short_result["phase"]["id"] == "bootstrap", "sample burst advanced bootstrap")
    require(
        "stable_window_wall_duration_incomplete" in short_result["readiness"]["blockers"],
        "short wall-duration blocker is missing",
    )

    paused_tracker = PhaseEvidenceTracker()
    paused_result: Dict[str, Any] = {}
    for index in range(1, PHASE_STABILITY_SAMPLES["bootstrap"] + 1):
        state = base_state()
        state["seq"] = index
        state["snapshot_seq"] = index
        state["snapshot_id"] = f"{state['session_id']}:{index}"
        state["ts"] = (start + dt.timedelta(seconds=index * 5)).isoformat(timespec="seconds")
        state["game_time_cycles"] = 200.5
        paused_result = paused_tracker.update(evaluate_fixture(state))
    require(paused_result["phase"]["id"] == "bootstrap", "paused game-time samples advanced bootstrap")
    require(
        "stable_window_game_duration_incomplete" in paused_result["readiness"]["blockers"],
        "paused game-duration blocker is missing",
    )

    gap_tracker = PhaseEvidenceTracker()
    first = base_state()
    first["seq"] = 1
    first["snapshot_seq"] = 1
    first["snapshot_id"] = f"{first['session_id']}:1"
    first["ts"] = (start + dt.timedelta(seconds=5)).isoformat(timespec="seconds")
    first["game_time_cycles"] = 200.1
    gap_tracker.update(evaluate_fixture(first))
    gap = base_state()
    gap["seq"] = 3
    gap["snapshot_seq"] = 3
    gap["snapshot_id"] = f"{gap['session_id']}:3"
    gap["ts"] = (start + dt.timedelta(seconds=15)).isoformat(timespec="seconds")
    gap["game_time_cycles"] = 200.3
    gap_result = gap_tracker.update(evaluate_fixture(gap))
    require(
        "strategy_snapshot_sequence_invalid" in gap_result["phase_evidence"]["integrity_blockers"],
        "snapshot sequence gap did not invalidate the evidence window",
    )
    require(gap_result["readiness"]["observed_consecutive_samples"] == 0, "gap retained samples")

    cadence_tracker = PhaseEvidenceTracker()
    cadence_first = base_state()
    cadence_first["seq"] = 1
    cadence_first["snapshot_seq"] = 1
    cadence_first["snapshot_id"] = f"{cadence_first['session_id']}:1"
    cadence_first["ts"] = (start + dt.timedelta(seconds=5)).isoformat(timespec="seconds")
    cadence_first["game_time_cycles"] = 200.1
    cadence_tracker.update(evaluate_fixture(cadence_first))
    cadence_late = base_state()
    cadence_late["seq"] = 2
    cadence_late["snapshot_seq"] = 2
    cadence_late["snapshot_id"] = f"{cadence_late['session_id']}:2"
    cadence_late["ts"] = (start + dt.timedelta(seconds=15)).isoformat(timespec="seconds")
    cadence_late["game_time_cycles"] = 200.2
    cadence_result = cadence_tracker.update(evaluate_fixture(cadence_late))
    require(
        "strategy_snapshot_gap" in cadence_result["phase_evidence"]["integrity_blockers"],
        "missing observation was tolerated as a gap-free cadence",
    )
    require(
        cadence_result["phase_evidence"]["evidence_window"]["gap_free"] is False,
        "cadence gap was labelled gap-free",
    )
    return {
        "sample_burst": "held",
        "paused_game_time": "held",
        "sequence_gap": "reset",
        "cadence_gap": "reset",
        "minimum_seconds": PHASE_STABILITY_MIN_SECONDS["bootstrap"],
        "minimum_game_cycles": PHASE_STABILITY_MIN_GAME_CYCLES["bootstrap"],
    }


def case_cycle_and_game_time_mismatch_fails_closed() -> Dict[str, Any]:
    tracker = PhaseEvidenceTracker()
    state = base_state()
    state["cycle"] = 201
    state["game_time_cycles"] = 200.5
    result = tracker.update(evaluate_fixture(state))
    blockers = result["phase_evidence"]["integrity_blockers"]
    require("strategy_cycle_mismatch" in blockers, "contradictory cycle telemetry was accepted")
    require(result["readiness"]["status"] != "ready", "mismatched cycle telemetry certified readiness")
    return {"blocker": "strategy_cycle_mismatch", "status": result["readiness"]["status"]}


def case_native_display_clock_boundaries() -> Dict[str, Any]:
    # Probe reads GetCycle()+1 and rounds GetTimeInCycles()+1 to six decimals.
    # The final fractional instant can therefore round to the next integer.
    accepted = ((1, 1.0), (1, 1.75), (200, 200.999999), (200, 201.0), (201, 201.0))
    for cycle, game_time in accepted:
        state = base_state()
        state.update(cycle=cycle, game_time_cycles=game_time)
        result = PhaseEvidenceTracker().update(evaluate_fixture(state))
        require(result["phase_evidence"]["integrity_blockers"] == [], f"native display clock rejected: {cycle}/{game_time}")
        require(result["phase_evidence"]["consecutive_samples"] == 1, "valid clock failed to enter evidence")
    rejected = ((1, 2.000001), (200, 201.000001), (201, 200.999999), (200, 202.0))
    for cycle, game_time in rejected:
        state = base_state()
        state.update(cycle=cycle, game_time_cycles=game_time)
        result = PhaseEvidenceTracker().update(evaluate_fixture(state))
        require("strategy_cycle_mismatch" in result["phase_evidence"]["integrity_blockers"], f"inconsistent clock accepted: {cycle}/{game_time}")
        require(result["phase_evidence"]["consecutive_samples"] == 0, "inconsistent clock accumulated evidence")
    return {"native_clock_cases": len(accepted), "inconsistent_clock_cases": len(rejected)}


def case_malformed_game_time_metadata_fails_closed() -> Dict[str, Any]:
    cases = (None, 0.0, 0.5, -1.0, float("nan"), float("inf"), float("-inf"), "200.5", {}, True)
    for game_time in cases:
        state = base_state()
        state["game_time_cycles"] = game_time
        result = PhaseEvidenceTracker().update(evaluate_fixture(state))
        require("strategy_game_time_invalid" in result["phase_evidence"]["integrity_blockers"], f"invalid game time accepted: {game_time!r}")
        require(result["phase_evidence"]["consecutive_samples"] == 0, "invalid game time accumulated evidence")
        json.dumps(result, allow_nan=False)
    return {"malformed_game_time_cases": len(cases)}


def case_negative_cycle_metadata_fails_closed() -> Dict[str, Any]:
    tracker = PhaseEvidenceTracker()
    state = base_state()
    state["cycle"] = -1
    state["game_time_cycles"] = -1.0
    result = tracker.update(evaluate_fixture(state))
    blockers = result["phase_evidence"]["integrity_blockers"]
    require("strategy_game_time_invalid" in blockers, "negative cycle metadata was accepted")
    require("strategy_cycle_invalid" in blockers, "negative cycle number was accepted")
    require(result["readiness"]["status"] != "ready", "negative cycle metadata certified readiness")
    return {"blockers": ["strategy_cycle_invalid", "strategy_game_time_invalid"], "status": result["readiness"]["status"]}


def case_malformed_cycle_metadata_fails_closed() -> Dict[str, Any]:
    cases = (1.5, float("nan"), float("inf"), "200", {"value": 200}, True)
    blockers_by_value: Dict[str, List[str]] = {}
    for value in cases:
        tracker = PhaseEvidenceTracker()
        state = base_state()
        state["cycle"] = value
        result = tracker.update(evaluate_fixture(state))
        blockers = result["phase_evidence"]["integrity_blockers"]
        require("strategy_cycle_invalid" in blockers, f"malformed cycle metadata was accepted: {value!r}")
        require(result["readiness"]["status"] != "ready", f"malformed cycle certified readiness: {value!r}")
        blockers_by_value[repr(value)] = blockers
    return {"cases": len(cases), "blockers": blockers_by_value}


def case_conflicting_duplicate_snapshot_fails_closed() -> Dict[str, Any]:
    tracker = PhaseEvidenceTracker()
    first = base_state()
    first_result = tracker.update(evaluate_fixture(first))
    require(first_result["phase_evidence"]["consecutive_samples"] == 1, "initial snapshot did not enter evidence")
    duplicate = copy.deepcopy(first)
    duplicate["resources"]["food_kcal"] = 1.0
    result = tracker.update(evaluate_fixture(duplicate))
    blockers = result["phase_evidence"]["integrity_blockers"]
    require("strategy_duplicate_conflict" in blockers, "conflicting duplicate snapshot was accepted")
    require(result["phase_evidence"]["consecutive_samples"] == 0, "conflicting duplicate retained proof window")
    require(result["readiness"]["status"] != "ready", "conflicting duplicate certified readiness")
    original_again = tracker.update(evaluate_fixture(first))
    require("strategy_duplicate_conflict" in original_again["phase_evidence"]["integrity_blockers"], "replayed original cleared a conflicting duplicate")
    direct_tracker = PhaseEvidenceTracker()
    direct_strategy = evaluate_fixture(first)
    direct_tracker.update(direct_strategy)
    tampered_strategy = copy.deepcopy(direct_strategy)
    tampered_strategy["metrics"]["food_kcal"] = 1.0
    changed = direct_tracker.update(tampered_strategy)
    require("strategy_duplicate_conflict" in changed["phase_evidence"]["integrity_blockers"], "changed strategy with unchanged state_hash bypassed duplicate detection")
    return {"blocker": "strategy_duplicate_conflict", "status": result["readiness"]["status"]}


def case_evidence_window_reports_critical_incidents() -> Dict[str, Any]:
    tracker = PhaseEvidenceTracker()
    state = base_state()
    first = tracker.update(evaluate_fixture(state))
    require(first["phase_evidence"]["evidence_window"]["critical_incidents"] == 0, "initial incident count was not zero")
    state["seq"] += 1
    state["snapshot_seq"] += 1
    state["snapshot_id"] = f"{state['session_id']}:{state['seq']}"
    state["ts"] = (dt.datetime.fromisoformat(state["ts"]) + dt.timedelta(seconds=5)).isoformat()
    state["game_time_cycles"] += 0.1
    state["alerts"] = ["low_oxygen"]
    state["quality"]["alerts"] = observed("alerts")
    result = tracker.update(evaluate_fixture(state))
    incidents = result["phase_evidence"]["evidence_window"]["critical_incidents"]
    require(incidents == 1, f"critical incident count was under-reported: {incidents}")
    duplicate = tracker.update(evaluate_fixture(state))
    require(duplicate["phase_evidence"]["critical_incident_count"] == 1, "duplicate counted another incident")
    return {"critical_incidents": incidents}


def case_clean_window_recovers_after_historical_incident() -> Dict[str, Any]:
    tracker = PhaseEvidenceTracker()
    start = dt.datetime.now(dt.timezone.utc)
    total = sum(PHASE_STABILITY_SAMPLES.values())

    def sample(index: int, *, incident: bool = False) -> Dict[str, Any]:
        state = base_state()
        state["seq"] = index * 2
        state["snapshot_seq"] = index
        state["snapshot_id"] = f"{state['session_id']}:{state['seq']}"
        state["ts"] = (start + dt.timedelta(seconds=index * 5)).isoformat(timespec="seconds")
        state["game_time_cycles"] = 200.0 + index * 0.1
        state["cycle"] = int(state["game_time_cycles"])
        if incident:
            state["resources"]["water_kg_estimate"] = 0.0
        return tracker.update(evaluate_fixture(state))

    for index in range(1, total + 1):
        initial = sample(index)
    initial_projection = _project_strategy(initial)
    require(initial_projection["readiness"]["status"] == "ready", "initial clean window failed real decision projection")
    require(initial_projection["readiness"]["evidence_window_valid"] is True, "initial clean window was invalid")

    incident = sample(total + 1, incident=True)
    require(incident["phase_evidence"]["critical_incident_count"] == 1, "one risk/regression counted multiple lifetime incidents")
    require(incident["phase_evidence"]["evidence_window"]["critical_incidents"] == 1, "incident sample did not mark the interrupted window")
    require(incident["phase_evidence"]["consecutive_samples"] == 0, "incident retained clean evidence")
    require(_project_strategy(incident)["readiness"]["status"] != "ready", "incident retained decision readiness")

    first_clean = sample(total + 2)
    require(first_clean["phase_evidence"]["critical_incident_count"] == 1, "new window erased historical incident")
    require(first_clean["phase_evidence"]["evidence_window"]["critical_incidents"] == 0, "historical incident contaminated new clean window")
    require(first_clean["phase_evidence"]["consecutive_samples"] == 1, "new clean window did not restart from one sample")
    require(_project_strategy(first_clean)["readiness"]["status"] != "ready", "one clean sample bypassed fresh-window thresholds")

    for index in range(total + 3, total * 2 + 2):
        recovered = sample(index)
    recovered_projection = _project_strategy(recovered)
    require(recovered["session_id"] == initial["session_id"], "recovery changed sessions")
    require(recovered["phase"]["id"] == "sustain", "fresh clean window did not recover sustain")
    require(recovered["phase_evidence"]["critical_incident_count"] == 1, "recovery erased lifetime incident diagnostic")
    require(recovered["phase_evidence"]["evidence_window"]["critical_incidents"] == 0, "recovery retained old window incident")
    require(recovered_projection["readiness"]["status"] == "ready", "historical incident permanently blocked decision readiness")
    require(recovered_projection["readiness"]["evidence_window_valid"] is True, "fresh recovered window failed real decision projection")

    contaminated = copy.deepcopy(recovered)
    contaminated["phase_evidence"]["evidence_window"]["critical_incidents"] = 1
    require(_project_strategy(contaminated)["readiness"]["status"] != "ready", "decision contract accepted a contaminated current window")
    return {
        "initial_safe_samples": total,
        "fresh_safe_samples_after_incident": total,
        "recovered_window_samples": recovered["phase_evidence"]["consecutive_samples"],
        "lifetime_incidents": 1,
        "current_window_incidents": 0,
        "decision_readiness": recovered_projection["readiness"]["status"],
        "decision_window_valid": recovered_projection["readiness"]["evidence_window_valid"],
        "same_session": True,
    }


def case_critical_risk_outside_active_gate_blocks_window() -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    for domain, expected_objective in (("water", "water_resilience"), ("industry_waste", "industry_waste_handling")):
        state = base_state()
        if domain == "water":
            state["resources"]["water_kg_estimate"] = 0.0
        else:
            state["industry"]["waste"]["handled"] = False
        strategy = evaluate_fixture(state)
        bootstrap_gate = next(item for item in strategy["gates"] if item["phase"] == "bootstrap")
        require(bootstrap_gate["status"] == "satisfied", "fixture did not isolate a risk outside the active gate")
        require(objective(strategy, expected_objective)["status"] == "at_risk", "fixture did not expose the critical risk")
        tracker = PhaseEvidenceTracker()
        tracked = tracker.update(strategy)
        require(tracked["phase_evidence"]["consecutive_samples"] == 0, "another phase's critical risk accumulated a clean sample")
        require(tracked["phase_evidence"]["critical_incident_count"] == 1, "critical risk outside active gate was not counted")
        require(tracked["phase_evidence"]["evidence_window"]["critical_incidents"] == 1, "risky sample was labeled a clean window")
        require(_project_strategy(tracked)["readiness"]["status"] != "ready", "cross-phase critical risk became decision-ready")
        duplicate = tracker.update(strategy)
        require(duplicate["phase_evidence"]["critical_incident_count"] == 1, "duplicate critical risk was counted twice")
        require(duplicate["phase_evidence"]["consecutive_samples"] == 0, "duplicate critical risk entered clean evidence")
        results[domain] = {"active_gate": "satisfied", "clean_samples": 0, "lifetime_incidents": 1, "window_incidents": 1}
    return results


def case_actual_probe_shape_remains_unproven() -> Dict[str, Any]:
    state = base_state()
    state["duplicants"].update({"total": 3, "stressed": None, "idle": None})
    state["resources"].update({"oxygen_kg_estimate": None, "water_kg_estimate": None})
    state["alerts"] = None
    state["research"]["complete"] = None
    for group in ("production", "consumption"):
        for key in state[group]:
            state[group][key] = None
    for group, key in (
        ("production", "completed"),
        ("delivery", "completed"),
        ("inputs", "supplied"),
        ("heat", "removed"),
        ("waste", "handled"),
    ):
        state["industry"][group][key] = None
    state["power"].update(
        {"battery_percent": None, "critical_load_covered": None, "independent_source_count": None}
    )
    state["temperature"].update({"critical_zones_safe": None, "min_c": None, "max_c": None})
    state["space"].update(
        {"mission_ready": None, "mission_completed": None, "return_ready": None, "base_reserve_safe": None}
    )
    state["resilience"]["n_minus_one_safe"] = None
    state["colony"]["sustainable"] = None
    for path in list(state["quality"]):
        if path.startswith(("production.", "consumption.", "industry.", "space.", "resilience.", "colony.sustainable")) or path in {
            "resources.oxygen_kg_estimate",
            "resources.water_kg_estimate",
            "alerts",
            "duplicants.stressed",
            "duplicants.idle",
            "research.complete",
            "power.battery_percent",
            "power.critical_load_covered",
            "power.independent_source_count",
            "oxygen.independent_source_count",
            "water.independent_source_count",
            "temperature.critical_zones_safe",
            "temperature.min_c",
            "temperature.max_c",
        }:
            state["quality"][path] = {"status": "unsupported", "source": "none"}
    for path in ("duplicants.total", "buildings.oxygen_diffuser", "buildings.outhouse", "buildings.manual_generator"):
        state["quality"][path] = {"status": "estimated", "source": "current_probe_shape"}

    result = evaluate_strategy(state)
    require(result["phase"]["detected_from_single_snapshot"] == "bootstrap", "actual Probe shape advanced")
    require(objective(result, "oxygen_security")["status"] == "unknown", "missing oxygen was certified")
    require(objective(result, "indefinite_survival")["status"] != "satisfied", "actual Probe shape certified sustain")
    return {"detected": "bootstrap", "oxygen": "unknown", "sustain": "unproven"}


def case_unsupported_sustainability_is_explicitly_blocked() -> Dict[str, Any]:
    state = base_state()
    state["colony"]["sustainable"] = None
    state["quality"]["colony.sustainable"] = {"status": "unsupported"}
    result = evaluate_strategy(state)
    require(
        "sustainability_telemetry_unsupported" in result["readiness"]["blockers"],
        "unsupported sustainability telemetry was not exposed as a readiness blocker",
    )
    require(
        objective(result, "indefinite_survival")["status"] != "satisfied",
        "unsupported sustainability telemetry certified indefinite survival",
    )
    require(
        objective(result, "indefinite_survival")["reason"]
        == "Sustainability telemetry is unsupported; indefinite survival cannot be certified from this observation.",
        "unsupported sustainability telemetry did not explain the certification block",
    )
    return {"blocker": "sustainability_telemetry_unsupported", "status": result["readiness"]["status"]}


def case_missing_sustainability_is_explicitly_unverified() -> Dict[str, Any]:
    state = base_state()
    state["colony"]["sustainable"] = None
    state["quality"]["colony.sustainable"] = {"status": "missing"}
    result = evaluate_strategy(state)
    require(
        "sustainability_telemetry_unverified" in result["readiness"]["blockers"],
        "missing sustainability telemetry was not exposed as a readiness blocker",
    )
    require(
        objective(result, "indefinite_survival")["status"] != "satisfied",
        "missing sustainability telemetry certified indefinite survival",
    )
    return {"blocker": "sustainability_telemetry_unverified", "status": result["readiness"]["status"]}


def case_false_sustainability_is_explicitly_unproven() -> Dict[str, Any]:
    state = base_state()
    state["colony"]["sustainable"] = False
    state["game"]["victory"] = None
    state["quality"]["game.victory"] = {"status": "unsupported"}
    result = evaluate_fixture(state)
    require(
        "sustainability_observed_false" in result["readiness"]["blockers"],
        "trusted false sustainability was not exposed as a readiness blocker",
    )
    require(
        objective(result, "indefinite_survival")["status"] == "at_risk",
        "false sustainability telemetry did not remain at risk",
    )
    require(
        objective(result, "indefinite_survival")["reason"]
        == "The sustainability marker is observed false; indefinite survival cannot be certified from this observation.",
        "false sustainability telemetry did not explain the certification block",
    )
    return {"status": objective(result, "indefinite_survival")["status"]}


def case_sustainability_false_blocker_requires_trusted_boolean() -> Dict[str, Any]:
    checked = 0
    for value, quality, verified in (
        (False, "observed", False),
        (False, "unsupported", True),
        (False, "estimated", True),
        (False, "stale", True),
        (False, "missing", True),
        (None, "observed", True),
        (0, "observed", True),
        ("false", "observed", True),
        (True, "observed", True),
    ):
        state = base_state()
        state["colony"]["sustainable"] = value
        state["quality"]["colony.sustainable"]["status"] = quality
        result = evaluate_fixture(state) if verified else evaluate_strategy(state)
        blockers = result["readiness"]["blockers"]
        require(
            "sustainability_observed_false" not in blockers,
            f"non-certifying false marker reported observed: {value!r}/{quality}/{verified}",
        )
        if value is not True:
            require(
                objective(result, "indefinite_survival")["status"] != "satisfied",
                "non-certifying sustainability marker certified indefinite survival",
            )
            require(
                "sustainability_telemetry_unsupported" in blockers
                or "sustainability_telemetry_unverified" in blockers,
                "unusable sustainability marker lost its uncertainty blocker",
            )
        checked += 1
    return {"trust_boundary_cases": checked, "live_admission": False}


def case_tracker_preserves_sustainability_blockers() -> Dict[str, Any]:
    cases = (
        (None, "unsupported", "sustainability_telemetry_unsupported"),
        (None, "missing", "sustainability_telemetry_unverified"),
        (False, "observed", "sustainability_observed_false"),
        (False, "estimated", "sustainability_telemetry_unverified"),
    )
    for value, quality, expected in cases:
        state = base_state()
        state["colony"]["sustainable"] = value
        state["quality"]["colony.sustainable"]["status"] = quality
        strategy = evaluate_fixture(state)
        tracked = PhaseEvidenceTracker().update(strategy)
        require(expected in strategy["readiness"]["blockers"], "snapshot lost sustainability blocker")
        require(expected in tracked["readiness"]["blockers"], "tracker rendering lost sustainability blocker")
        require(tracked["readiness"]["status"] != "ready", "uncertified sustainability became ready")
    return {"tracker_blocker_cases": len(cases)}


def case_functional_outhouse_gate_fail_closed() -> Dict[str, Any]:
    false_state = add_functional_outhouses(base_state(), [False])
    false_metrics = collect_metrics(false_state)
    require(
        false_metrics["functional_outhouse_operational"] is False,
        "observed non-operational Outhouse did not project false",
    )
    require(
        false_metrics["paths"]["functional_outhouse_operational"] == "functional_buildings",
        "functional Outhouse path was not exposed",
    )
    require(
        false_metrics["qualities"]["functional_outhouse_operational"] == "observed",
        "observed functional Outhouse quality was lost",
    )
    false_result = evaluate_fixture(false_state)
    false_sanitation = objective(false_result, "sanitation")
    require(false_sanitation["status"] == "at_risk", "operational=false was certified as safe")
    require("operational=false" in false_sanitation["reason"], "failure reason omitted operational=false")
    require("functional_buildings" in false_sanitation["evidence"], "failure evidence omitted functional telemetry")

    true_state = add_functional_outhouses(base_state(), [True])
    true_metrics = collect_metrics(true_state)
    require(true_metrics["functional_outhouse_operational"] is True, "operational Outhouse did not project true")
    require(true_metrics["qualities"]["functional_outhouse_operational"] == "observed", "true quality was not observed")
    true_sanitation = objective(evaluate_fixture(true_state), "sanitation")
    require(true_sanitation["status"] == "satisfied", "valid operational telemetry blocked compatible coverage")

    mixed_state = add_functional_outhouses(base_state(), [True, False])
    mixed_metrics = collect_metrics(mixed_state)
    require(mixed_metrics["functional_outhouse_operational"] is False, "mixed operational values did not fail closed")
    require(objective(evaluate_fixture(mixed_state), "sanitation")["status"] == "at_risk", "mixed failure was certified")

    null_state = add_functional_outhouses(base_state(), [True, None])
    null_metrics = collect_metrics(null_state)
    require(null_metrics["functional_outhouse_operational"] is None, "null operational value became a proof")
    require(null_metrics["qualities"]["functional_outhouse_operational"] == "unknown", "null operational quality was not unknown")
    require(objective(evaluate_fixture(null_state), "sanitation")["status"] == "unknown", "null operational data satisfied sanitation")

    empty_state = add_functional_outhouses(base_state(), [])
    empty_metrics = collect_metrics(empty_state)
    require(empty_metrics["functional_outhouse_operational"] is None, "empty functional set became a proof")
    require(objective(evaluate_fixture(empty_state), "sanitation")["status"] == "unknown", "empty functional set satisfied sanitation")

    invalid_cases: Dict[str, Dict[str, Any]] = {}
    invalid_cases["incomplete"] = add_functional_outhouses(base_state(), [True], complete=False)
    invalid_cases["wrong_world"] = add_functional_outhouses(
        base_state(), [True], functional_world_id=7
    )
    stale_state = add_functional_outhouses(base_state(), [True])
    stale_at = dt.datetime.fromisoformat(stale_state["ts"]) - dt.timedelta(seconds=6)
    stale_state["functional_buildings"]["captured_at"] = stale_at.isoformat(timespec="seconds")
    invalid_cases["stale"] = stale_state
    malformed_state = add_functional_outhouses(base_state(), [True])
    malformed_state["functional_buildings"]["instances"][0]["operational"] = "yes"
    invalid_cases["malformed"] = malformed_state
    estimated_state = add_functional_outhouses(base_state(), [True])
    estimated_state["quality"]["functional_buildings"]["status"] = "estimated"
    invalid_cases["estimated"] = estimated_state
    missing_quality_state = add_functional_outhouses(base_state(), [True], include_quality=False)
    invalid_cases["missing_quality"] = missing_quality_state

    invalid_statuses: Dict[str, str] = {}
    for label, state in invalid_cases.items():
        metrics = collect_metrics(state)
        require(metrics["functional_outhouse_operational"] is None, f"{label} functional value was trusted")
        require(
            metrics["paths"]["functional_outhouse_operational"] == "functional_buildings",
            f"{label} functional path was hidden",
        )
        require(
            metrics["qualities"]["functional_outhouse_operational"] in {"contract_mismatch", "stale"},
            f"{label} did not expose a blocked functional quality",
        )
        invalid_statuses[label] = objective(evaluate_fixture(state), "sanitation")["status"]
        require(invalid_statuses[label] == "unknown", f"{label} functional data changed sanitation to a proof")

    missing_state = base_state()
    missing_metrics = collect_metrics(missing_state)
    require(missing_metrics["functional_outhouse_operational"] is None, "missing functional field changed tri-state")
    require(missing_metrics["paths"]["functional_outhouse_operational"] is None, "missing functional field fabricated a path")
    require(missing_metrics["qualities"]["functional_outhouse_operational"] == "unsupported", "missing functional field lost compatibility quality")
    require(
        objective(evaluate_fixture(missing_state), "sanitation")["status"] == "satisfied",
        "fully missing functional field broke legacy count-only compatibility",
    )
    return {
        "false": "at_risk",
        "true": "satisfied",
        "mixed": "at_risk",
        "null_and_empty": "unknown",
        "invalid": invalid_statuses,
        "missing_functional_field": "legacy_count_only",
    }


def case_functional_oxygen_and_power_gates() -> Dict[str, Any]:
    diffuser_off = add_functional_bootstrap_buildings(base_state(), diffuser=[False])
    oxygen_off = objective(evaluate_fixture(diffuser_off), "oxygen_security")
    require(oxygen_off["status"] == "at_risk", "non-operational diffuser was certified as safe")
    require("operational=false" in oxygen_off["reason"], "diffuser failure reason was not explicit")

    diffuser_unknown = add_functional_bootstrap_buildings(base_state(), diffuser=[None])
    oxygen_unknown = objective(evaluate_fixture(diffuser_unknown), "oxygen_security")
    require(oxygen_unknown["status"] == "unknown", "unknown diffuser telemetry became a proof")

    generator_off = add_functional_bootstrap_buildings(base_state(), generator=[False])
    power_off = objective(evaluate_fixture(generator_off), "power_resilience")
    require(power_off["status"] == "at_risk", "non-operational generator was certified as safe")
    require("operational=false" in power_off["reason"], "generator failure reason was not explicit")

    generator_unknown = add_functional_bootstrap_buildings(base_state(), generator=[None])
    power_unknown = objective(evaluate_fixture(generator_unknown), "power_resilience")
    require(power_unknown["status"] == "unknown", "unknown generator telemetry became a proof")

    compatible = add_functional_bootstrap_buildings(base_state(), diffuser=[True], generator=[True])
    compatible_result = evaluate_fixture(compatible)
    require(objective(compatible_result, "oxygen_security")["status"] == "satisfied", "operational diffuser blocked compatible oxygen")
    require(objective(compatible_result, "power_resilience")["status"] == "satisfied", "operational generator blocked compatible power")
    return {
        "diffuser_false": oxygen_off["status"],
        "diffuser_null": oxygen_unknown["status"],
        "generator_false": power_off["status"],
        "generator_null": power_unknown["status"],
        "operational_pair": "satisfied",
    }


def case_observe_and_update_equivalent() -> Dict[str, Any]:
    start = dt.datetime.now(dt.timezone.utc)
    rendered_tracker = PhaseEvidenceTracker()
    observed_tracker = PhaseEvidenceTracker()
    last_strategy: Dict[str, Any] = {}
    rendered: Dict[str, Any] = {}
    for index in range(1, 20):
        state = base_state()
        state["seq"] = index
        state["snapshot_seq"] = index
        state["snapshot_id"] = f"{state['session_id']}:{index}"
        state["ts"] = (start + dt.timedelta(seconds=index * 5)).isoformat(timespec="seconds")
        state["game_time_cycles"] = 200.0 + index * 0.1
        state["cycle"] = int(state["game_time_cycles"])
        last_strategy = evaluate_fixture(state)
        rendered = rendered_tracker.update(last_strategy)
        observed_tracker.observe(last_strategy)
    observed = observed_tracker.update(last_strategy)
    require(observed["phase"] == rendered["phase"], "observe/update phase state diverged")
    require(observed["readiness"] == rendered["readiness"], "observe/update readiness diverged")
    require(observed["phase_evidence"] == rendered["phase_evidence"], "observe/update evidence diverged")
    return {"samples": 19, "equivalent": True}


def case_session_bound_rolling_evidence() -> Dict[str, Any]:
    tracker = PhaseEvidenceTracker()
    start = dt.datetime.now(dt.timezone.utc)
    total = sum(PHASE_STABILITY_SAMPLES.values())
    tracked: Dict[str, Any] = {}
    for index in range(1, total + 1):
        state = base_state()
        # Global record sequence may legally skip because non-snapshot events
        # share it; snapshot_seq is the gap-free evidence clock.
        state["seq"] = index * 2
        state["snapshot_seq"] = index
        state["snapshot_id"] = f"{state['session_id']}:{state['seq']}"
        state["ts"] = (start + dt.timedelta(seconds=index * 5)).isoformat(timespec="seconds")
        state["game_time_cycles"] = 200.0 + index * 0.1
        state["cycle"] = int(state["game_time_cycles"])
        tracked = tracker.update(evaluate_fixture(state))

    require(tracked["phase"]["id"] == "sustain", "rolling evidence did not reach sustain")
    require(tracked["readiness"]["status"] == "ready", "sustain window was not certified")
    require(tracked["readiness"]["stable_window_verified"] is True, "sustain proof window is missing")

    duplicate_count = tracked["readiness"]["observed_consecutive_samples"]
    tracked_duplicate = tracker.update(evaluate_fixture(state))
    require(
        tracked_duplicate["readiness"]["observed_consecutive_samples"] == duplicate_count,
        "duplicate render increased phase evidence",
    )

    regression = base_state()
    regression["seq"] = total * 2 + 2
    regression["snapshot_seq"] = total + 1
    regression["snapshot_id"] = f"{regression['session_id']}:{regression['seq']}"
    regression["ts"] = (start + dt.timedelta(seconds=(total + 1) * 5)).isoformat(timespec="seconds")
    regression["game_time_cycles"] = 200.0 + (total + 1) * 0.1
    regression["cycle"] = int(regression["game_time_cycles"])
    regression["resources"]["water_kg_estimate"] = 0.0
    regressed = tracker.update(evaluate_fixture(regression))
    require(regressed["phase"]["id"] == "stabilize", "critical regression was not immediate")
    require(regressed["readiness"]["status"] != "ready", "regressed strategy remained ready")
    require(regressed["phase_evidence"]["critical_incident_count"] == 1, "one regression counted multiple incidents")
    require(regressed["phase_evidence"]["evidence_window"]["critical_incidents"] == 1, "rendered incident count differs")
    duplicate_regression = tracker.update(evaluate_fixture(regression))
    require(duplicate_regression["phase_evidence"]["critical_incident_count"] == 1, "duplicate regression counted again")

    new_session = base_state()
    new_session["session_id"] = "session-strategy-new"
    new_session["seq"] = 1
    new_session["snapshot_seq"] = 1
    new_session["snapshot_id"] = "session-strategy-new:1"
    new_session["ts"] = (start + dt.timedelta(seconds=(total + 2) * 5)).isoformat(timespec="seconds")
    new_session["game_time_cycles"] = 300.0
    new_session["cycle"] = 300
    reset = tracker.update(evaluate_fixture(new_session))
    require(reset["phase"]["id"] == "bootstrap", "phase evidence crossed session boundary")
    require(reset["phase_evidence"]["critical_incident_count"] == 0, "incident count crossed session boundary")
    require(reset["readiness"]["observed_consecutive_samples"] == 1, "new session did not reset evidence")
    return {"total_samples": total, "ready": True, "regressed_to": "stabilize", "session_reset": True}


def case_strict_operational_fields_fail_closed() -> Dict[str, Any]:
    expected_contracts = {
        "alerts": {
            "semantics": ("canonical_active_alert_ids",),
            "units": ("alert_id_list",),
            # All-world alerts (review G3/M8); the active-world form still
            # reads so older recordings replay.
            "scopes": ("all_worlds", "active_world"),
            "completeness": ("complete",),
            "sources": (
                "Components.LiveMinionIdentities.Items+GetMyWorldId+typed_monitor_predicates;catalog=oni.alert.catalog.all_worlds.v1",
                "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+typed_monitor_predicates;catalog=oni.alert.catalog.active_world.v1",
            ),
            "captures": ("snapshot.ts",),
            "contract_ids": (ALERTS_CANONICAL_CONTRACT_ID,),
        },
        "duplicants.idle": {
            "semantics": ("idle_count",),
            "units": ("count",),
            "scopes": ("active_world",),
            "completeness": ("complete",),
            "sources": (
                "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+GameTags.Idle",
            ),
            "captures": ("snapshot.ts",),
            "contract_ids": (DUPLICANTS_IDLE_CONTRACT_ID,),
        },
        "duplicants.stressed": {
            "semantics": ("stressed_count",),
            "units": ("count",),
            "scopes": ("active_world",),
            "completeness": ("complete",),
            "sources": (
                "Components.LiveMinionIdentities.GetWorldItems(activeWorldId,false)+StressMonitor",
            ),
            "captures": ("snapshot.ts",),
            "contract_ids": (DUPLICANTS_STRESSED_CONTRACT_ID,),
        },
    }
    for path, expected in expected_contracts.items():
        actual = CERTIFYING_FIELD_CONTRACTS[path]
        for key, value in expected.items():
            require(actual.get(key) == value, f"{path} {key} drifted from the shared field contract")

    canonical = base_state()
    canonical["alerts"] = [
        "bionic_low_battery",
        "hyperthermia",
        "low_oxygen",
        "starvation",
    ]
    canonical_metrics = collect_metrics(canonical)
    require(canonical_metrics["alerts"] == canonical["alerts"], "canonical alerts were transformed")
    require(canonical_metrics["qualities"]["alerts"] == "observed", "canonical alerts lost observed quality")
    for flag in ("low_oxygen_alert", "food_alert", "power_alert", "temperature_alert"):
        require(canonical_metrics[flag] is True, f"exact canonical mapping did not set {flag}")

    unrelated = base_state()
    unrelated["alerts"] = [
        "exhaustion",
        "high_stress",
        "incapacitated",
        "major_sickness",
        "sickness",
        "urgent_toilet",
        "wounded",
    ]
    unrelated_metrics = collect_metrics(unrelated)
    for flag in ("low_oxygen_alert", "food_alert", "power_alert", "temperature_alert"):
        require(unrelated_metrics[flag] is False, f"substring matching falsely set {flag}")

    malformed_alerts = {
        "unknown": ["unknown_hazard"],
        "unsorted": ["starvation", "low_oxygen"],
        "duplicate": ["low_oxygen", "low_oxygen"],
        "string": "low_oxygen",
        "set": {"low_oxygen"},
    }
    for label, value in malformed_alerts.items():
        state = base_state()
        state["alerts"] = value
        metrics = collect_metrics(state)
        require(metrics["alerts"] is None, f"{label} alerts remained usable")
        require(metrics["paths"]["alerts"] == "alerts", f"{label} alerts lost the primary diagnostic path")
        require(
            metrics["qualities"]["alerts"] == "contract_mismatch",
            f"{label} alerts did not fail closed",
        )
        require(
            all(
                metrics[flag] is None
                for flag in ("low_oxygen_alert", "food_alert", "power_alert", "temperature_alert")
            ),
            f"{label} alerts were collapsed into a false no-hazard result",
        )
        if label in {"unknown", "unsorted"}:
            result = evaluate_fixture(state)
            for objective_id in ("oxygen_security", "power_resilience", "thermal_safety"):
                require(
                    objective(result, objective_id)["status"] == "unknown",
                    f"{label} alerts did not make {objective_id} unknown",
                )

    legacy = base_state()
    legacy["colony"].update({"idle": 4, "stressed": 5})
    legacy["quality"]["colony.idle"] = observed("colony.idle")
    legacy["quality"]["colony.stressed"] = observed("colony.stressed")
    del legacy["duplicants"]["idle"]
    del legacy["duplicants"]["stressed"]
    del legacy["quality"]["duplicants.idle"]
    del legacy["quality"]["duplicants.stressed"]
    legacy_metrics = collect_metrics(legacy)
    require(legacy_metrics["idle"] == 4, "fully absent primary idle did not use its legacy alias")
    require(legacy_metrics["stressed"] == 5, "fully absent primary stress did not use its legacy alias")
    require(legacy_metrics["paths"]["idle"] == "colony.idle", "idle legacy path was not reported")
    require(legacy_metrics["paths"]["stressed"] == "colony.stressed", "stress legacy path was not reported")

    blocked_fallback = base_state()
    blocked_fallback["colony"].update({"idle": 7, "stressed": 8})
    blocked_fallback["quality"]["colony.idle"] = observed("colony.idle")
    blocked_fallback["quality"]["colony.stressed"] = observed("colony.stressed")
    blocked_fallback["quality"]["duplicants.idle"]["source"] = "legacy_spoof"
    blocked_fallback["quality"]["duplicants.stressed"]["contract_id"] = DUPLICANTS_IDLE_CONTRACT_ID
    blocked_metrics = collect_metrics(blocked_fallback)
    require(blocked_metrics["idle"] is None, "bad primary idle fell back to a legacy alias")
    require(blocked_metrics["stressed"] is None, "bad primary stress fell back to a legacy alias")
    require(blocked_metrics["paths"]["idle"] == "duplicants.idle", "bad idle hid its primary path")
    require(blocked_metrics["paths"]["stressed"] == "duplicants.stressed", "bad stress hid its primary path")
    require(blocked_metrics["qualities"]["idle"] == "contract_mismatch", "bad idle was not unknown")
    require(blocked_metrics["qualities"]["stressed"] == "contract_mismatch", "bad stress was not unknown")

    quality_only_primary = copy.deepcopy(legacy)
    quality_only_primary["quality"]["duplicants.idle"] = observed("duplicants.idle")
    quality_only_metrics = collect_metrics(quality_only_primary)
    require(quality_only_metrics["idle"] is None, "primary idle quality without a value used the legacy alias")
    require(
        quality_only_metrics["paths"]["idle"] == "duplicants.idle",
        "quality-only primary idle hid its diagnostic path",
    )

    return {
        "strict_contracts": sorted(expected_contracts),
        "canonical_alert_ids": 14,
        "malformed_alert_cases": sorted(malformed_alerts),
        "legacy_alias": "missing_only",
    }


def case_food_demand_finite_arithmetic() -> Dict[str, Any]:
    for population in (1e306, 1e308):
        state = base_state(); state["duplicants"]["total"] = population
        result = evaluate_fixture(state)
        for metric in ("food_runway_cycles", "food_deficit_kcal_per_cycle", "food_depletion_cycles"):
            require(result["metrics"][metric] is None, f"overflowing demand produced {metric}")
        require(objective(result, "renewable_food")["status"] == "unknown", "overflowing demand established balance")
        json.dumps(result, allow_nan=False)
    finite = base_state(); finite["duplicants"]["total"] = 1e305
    result = evaluate_fixture(finite)
    require(result["metrics"]["food_deficit_kcal_per_cycle"] == 1e308, "finite large demand was rejected")
    return {"overflow_populations_checked": 2, "finite_boundary_checked": True, "live_admission": False}


def case_food_depletion_forecast() -> Dict[str, Any]:
    state = base_state()
    state["production"]["food_kcal_per_cycle"] = 2_000.0
    result = evaluate_fixture(state)
    require(result["metrics"]["food_deficit_kcal_per_cycle"] == 1_000.0, "net deficit differs")
    require(result["metrics"]["food_depletion_cycles"] == 30.0, "net depletion differs")
    require(result["metrics"]["food_runway_cycles"] == 10.0, "gross reserve runway changed")
    require("forecast, not a safety certificate" in objective(result, "renewable_food")["reason"], "forecast caveat absent")
    require(objective(result, "renewable_food")["status"] == "at_risk", "forecast widened certification")
    for rate in (0.0, 3_000.0, 4_000.0):
        sample = copy.deepcopy(state)
        sample["production"]["food_kcal_per_cycle"] = rate
        metrics = evaluate_fixture(sample)["metrics"]
        require(metrics["food_depletion_cycles"] == (10.0 if rate == 0 else None), "zero or surplus handling differs")
        json.dumps(metrics, allow_nan=False)
    for path in ("production.food_kcal_per_cycle", "duplicants.total", "resources.food_kcal"):
        sample = copy.deepcopy(state)
        sample["quality"][path] = {"status": "unsupported"}
        require(evaluate_fixture(sample)["metrics"]["food_depletion_cycles"] is None, f"unsupported {path} forecast depletion")
    registered = copy.deepcopy(state)
    set_registered_food_quality(registered)
    require(evaluate_fixture(registered)["metrics"]["food_depletion_cycles"] is None, "registered stock treated as accessible")
    require(evaluate_strategy(state)["metrics"]["food_depletion_cycles"] is None, "self-declared production proof forecast depletion")
    empty = copy.deepcopy(state)
    empty["resources"]["food_kcal"] = 0.0
    require(evaluate_fixture(empty)["metrics"]["food_depletion_cycles"] == 0.0, "empty stock did not deplete immediately")
    overflow = copy.deepcopy(state)
    overflow["resources"]["food_kcal"] = 1e308
    overflow["production"]["food_kcal_per_cycle"] = 2_999.9999999999995
    overflow_metrics = evaluate_fixture(overflow)["metrics"]
    require(overflow_metrics["food_depletion_cycles"] is None, "non-finite forecast was emitted")
    json.dumps(overflow_metrics, allow_nan=False)
    for population in (0, None, True, 2.5):
        sample = copy.deepcopy(state)
        sample["duplicants"]["total"] = population
        require(evaluate_fixture(sample)["metrics"]["food_depletion_cycles"] is None, "invalid population produced forecast")
    return {"net_depletion_cycles": 30.0, "gross_runway_cycles": 10.0, "live_admission": False}


def case_oxygen_deficit_quantification() -> Dict[str, Any]:
    state = base_state()
    state["production"]["oxygen_kg_per_cycle"] = 100.0
    result = evaluate_fixture(state)
    require(result["metrics"]["oxygen_deficit_kg_per_cycle"] == 80.0, "trusted oxygen deficit missing")
    risk = objective(result, "renewable_oxygen")
    require(risk["status"] == "at_risk", "oxygen shortfall did not retain risk gate")
    require("80.0 kg/cycle" in risk["reason"] and "time to suffocation" in risk["reason"], "quantified shortfall lacks reserve caveat")
    require(objective(result, "oxygen_security")["status"] == "at_risk", "oxygen security gate changed")
    require(evaluate_strategy(state)["metrics"]["oxygen_deficit_kg_per_cycle"] is None, "self-declared proof created deficit")
    for production, demand, expected in ((0.0, 180.0, 180.0), (180.0, 180.0, 0.0), (200.0, 180.0, 0.0), (0.0, 0.0, 0.0), (0.0, 1e308, 1e308)):
        sample = copy.deepcopy(state)
        sample["production"]["oxygen_kg_per_cycle"] = production
        sample["consumption"]["oxygen_kg_per_cycle"] = demand
        metrics = evaluate_fixture(sample)["metrics"]
        require(metrics["oxygen_deficit_kg_per_cycle"] == expected, "incorrect oxygen balance")
        json.dumps(metrics, allow_nan=False)
    for section in ("production", "consumption"):
        path = f"{section}.oxygen_kg_per_cycle"
        for invalid in (None, True, "180", {}, -1.0, float("nan"), float("inf")):
            sample = copy.deepcopy(state)
            sample[section]["oxygen_kg_per_cycle"] = invalid
            require(evaluate_fixture(sample)["metrics"]["oxygen_deficit_kg_per_cycle"] is None, "invalid rate produced deficit")
        for field, value in (("status", "estimated"), ("status", "missing"), ("scope", "wrong_scope"), ("input_hash", "invalid"), ("source", "untrusted")):
            sample = copy.deepcopy(state)
            sample["quality"][path][field] = value
            require(evaluate_fixture(sample)["metrics"]["oxygen_deficit_kg_per_cycle"] is None, "untrusted rate produced deficit")
        sample = copy.deepcopy(state)
        del sample["quality"][path]
        require(evaluate_fixture(sample)["metrics"]["oxygen_deficit_kg_per_cycle"] is None, "missing quality produced deficit")
    stale = copy.deepcopy(state)
    stale["bridge"]["stale"] = True
    stale_risk = objective(evaluate_fixture(stale), "renewable_oxygen")
    require(stale_risk["status"] == "blocked" and "80.0 kg/cycle" not in stale_risk["reason"], "stale snapshot presented actionable deficit")
    return {"deficit_kg_per_cycle": 80.0, "live_admission": False}


def case_power_deficit_quantification() -> Dict[str, Any]:
    state = base_state()
    state["production"]["power_watts"] = 100.0
    state["consumption"]["power_watts"] = 180.0
    result = evaluate_fixture(state)
    require(result["metrics"]["power_deficit_watts"] == 80.0, "trusted power deficit missing")
    risk = objective(result, "renewable_power")
    require(risk["status"] == "at_risk", "power shortfall did not retain risk gate")
    require("80.0 W" in risk["reason"] and "time to depletion" in risk["reason"], "shortfall lacks battery reserve caveat")
    require(evaluate_strategy(state)["metrics"]["power_deficit_watts"] is None, "self-declared proof created deficit")
    for production, demand, expected in ((0.0, 180.0, 180.0), (180.0, 180.0, 0.0), (200.0, 180.0, 0.0), (0.0, 0.0, 0.0), (0.0, 1e308, 1e308)):
        sample = copy.deepcopy(state)
        sample["production"]["power_watts"] = production
        sample["consumption"]["power_watts"] = demand
        metrics = evaluate_fixture(sample)["metrics"]
        require(metrics["power_deficit_watts"] == expected, "incorrect power balance")
        json.dumps(metrics, allow_nan=False)
    for section in ("production", "consumption"):
        path = f"{section}.power_watts"
        for invalid in (None, True, "180", {}, -1.0, float("nan"), float("inf")):
            sample = copy.deepcopy(state)
            sample[section]["power_watts"] = invalid
            require(evaluate_fixture(sample)["metrics"]["power_deficit_watts"] is None, "invalid rate produced deficit")
        for field, value in (("status", "estimated"), ("status", "missing"), ("scope", "wrong_scope"), ("unit", "J"), ("input_hash", "invalid"), ("source", "untrusted")):
            sample = copy.deepcopy(state)
            sample["quality"][path][field] = value
            require(evaluate_fixture(sample)["metrics"]["power_deficit_watts"] is None, "untrusted rate produced deficit")
        sample = copy.deepcopy(state)
        del sample["quality"][path]
        require(evaluate_fixture(sample)["metrics"]["power_deficit_watts"] is None, "missing quality produced deficit")
    stale = copy.deepcopy(state)
    stale["bridge"]["stale"] = True
    stale_risk = objective(evaluate_fixture(stale), "renewable_power")
    require(stale_risk["status"] == "blocked" and "80.0 W" not in stale_risk["reason"], "stale snapshot presented actionable deficit")
    return {"deficit_watts": 80.0, "live_admission": False}


def case_water_deficit_quantification() -> Dict[str, Any]:
    state = base_state()
    state["production"]["water_kg_per_cycle"] = 100.0
    state["consumption"]["water_kg_per_cycle"] = 180.0
    result = evaluate_fixture(state)
    require(result["metrics"]["water_deficit_kg_per_cycle"] == 80.0, "trusted water deficit missing")
    risk = objective(result, "renewable_water")
    require(risk["status"] == "at_risk", "water shortfall did not retain risk gate")
    require("80.0 kg/cycle" in risk["reason"] and "time to depletion" in risk["reason"], "quantified shortfall lacks reserve caveat")
    require(evaluate_strategy(state)["metrics"]["water_deficit_kg_per_cycle"] is None, "self-declared proof created deficit")
    for production, demand, expected in ((0.0, 180.0, 180.0), (180.0, 180.0, 0.0), (200.0, 180.0, 0.0), (0.0, 0.0, 0.0), (0.0, 1e308, 1e308)):
        sample = copy.deepcopy(state)
        sample["production"]["water_kg_per_cycle"] = production
        sample["consumption"]["water_kg_per_cycle"] = demand
        metrics = evaluate_fixture(sample)["metrics"]
        require(metrics["water_deficit_kg_per_cycle"] == expected, "incorrect water balance")
        json.dumps(metrics, allow_nan=False)
    for section in ("production", "consumption"):
        path = f"{section}.water_kg_per_cycle"
        for invalid in (None, True, "180", {}, -1.0, float("nan"), float("inf")):
            sample = copy.deepcopy(state)
            sample[section]["water_kg_per_cycle"] = invalid
            require(evaluate_fixture(sample)["metrics"]["water_deficit_kg_per_cycle"] is None, "invalid rate produced deficit")
        for field, value in (("status", "estimated"), ("status", "missing"), ("scope", "wrong_scope"), ("input_hash", "invalid"), ("source", "untrusted")):
            sample = copy.deepcopy(state)
            sample["quality"][path][field] = value
            require(evaluate_fixture(sample)["metrics"]["water_deficit_kg_per_cycle"] is None, "untrusted rate produced deficit")
        sample = copy.deepcopy(state)
        del sample[section]["water_kg_per_cycle"]
        require(evaluate_fixture(sample)["metrics"]["water_deficit_kg_per_cycle"] is None, "missing rate produced deficit")
        sample = copy.deepcopy(state)
        del sample["quality"][path]
        require(evaluate_fixture(sample)["metrics"]["water_deficit_kg_per_cycle"] is None, "missing quality produced deficit")
    stale = copy.deepcopy(state)
    stale["bridge"]["stale"] = True
    stale_risk = objective(evaluate_fixture(stale), "renewable_water")
    require(stale_risk["status"] == "blocked" and "80.0 kg/cycle" not in stale_risk["reason"], "stale snapshot presented actionable deficit")
    return {"deficit_kg_per_cycle": 80.0, "live_admission": False}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, default=ROOT / "reports" / "strategy-verification.json")
    args = parser.parse_args(argv)
    cases = [
        ("historical_incident_clean_window_recovery", case_clean_window_recovers_after_historical_incident),
        ("cross_phase_critical_incident_window", case_critical_risk_outside_active_gate_blocks_window),
        ("native_display_clock_boundaries", case_native_display_clock_boundaries),
        ("malformed_game_time_metadata", case_malformed_game_time_metadata_fails_closed),
        ("tracker_sustainability_blockers", case_tracker_preserves_sustainability_blockers),
        ('cycle_game_time_consistency', case_cycle_and_game_time_mismatch_fails_closed),
        ('negative_cycle_metadata', case_negative_cycle_metadata_fails_closed),
        ('malformed_cycle_metadata', case_malformed_cycle_metadata_fails_closed),
        ('conflicting_duplicate_snapshot', case_conflicting_duplicate_snapshot_fails_closed),
        ('unsupported_sustainability', case_unsupported_sustainability_is_explicitly_blocked),
        ('missing_sustainability', case_missing_sustainability_is_explicitly_unverified),
        ('false_sustainability', case_false_sustainability_is_explicitly_unproven),
        ('false_sustainability_trust_boundary', case_sustainability_false_blocker_requires_trusted_boolean),
        ('critical_incident_accounting', case_evidence_window_reports_critical_incidents),
        ("food_demand_finite_arithmetic", case_food_demand_finite_arithmetic),
        ("power_deficit_quantification", case_power_deficit_quantification),
        ("water_deficit_quantification", case_water_deficit_quantification),
        ("oxygen_deficit_quantification", case_oxygen_deficit_quantification),
        ("food_depletion_forecast", case_food_depletion_forecast),
        ("happy_single_snapshot", case_happy_single_snapshot_is_not_certification),
        ("critical_domains", case_critical_domains_block),
        ("strategy_cannot_issue_pause", case_strategy_cannot_issue_pause),
        ("research_action_adapter_selection", case_research_action_requires_adapter_selection),
        ("rate_balance_markers", case_rate_balance_and_markers_fail_closed),
        ("estimated_fail_closed", case_estimated_cannot_certify),
        ("visible_cell_risk_only", case_visible_cell_aggregate_is_risk_only),
        ("registered_food_risk_only", case_registered_food_is_risk_only),
        ("trust_cycle", case_trust_and_cycle_fail_closed),
        ("contract_scope_marker_spoofs", case_contract_scope_and_marker_spoofs_fail_closed),
        ("duration_gap_game_time", case_duration_gap_and_game_time_fail_closed),
        ("actual_probe_shape", case_actual_probe_shape_remains_unproven),
        ("functional_outhouse_gate", case_functional_outhouse_gate_fail_closed),
        ("functional_oxygen_power_gates", case_functional_oxygen_and_power_gates),
        ("strict_operational_fields", case_strict_operational_fields_fail_closed),
        ("rolling_evidence", case_session_bound_rolling_evidence),
        ("observe_update_equivalence", case_observe_and_update_equivalent),
    ]
    results: List[Dict[str, Any]] = []
    for name, function in cases:
        try:
            details = function()
            results.append({"name": name, "status": "passed", "details": details})
            print(f"[PASSED] {name}: {json.dumps(details, ensure_ascii=False, sort_keys=True)}")
        except Exception as exc:
            results.append({"name": name, "status": "failed", "error": str(exc)})
            print(f"[FAILED] {name}: {exc}")
    passed = all(item["status"] == "passed" for item in results)
    strategy_source = pathlib.Path(strategy_module.__file__).resolve()
    report = {
        "schema": "oni.strategy-verification.v1",
        "status": "passed" if passed else "failed",
        "test_count": len(results),
        "exit_code": 0 if passed else 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "module_origins": {
            "test_module": str(pathlib.Path(__file__).resolve()),
            "strategy_module": str(strategy_source),
        },
        "strategy_sha256": hashlib.sha256(strategy_source.read_bytes()).hexdigest(),
        "live_admission": False,
        "pycache_created": False,
        "phase_exit_gates": {key: list(value) for key, value in PHASE_EXIT_GATES.items()},
        "cases": results,
    }
    path = args.report.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Report: {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
