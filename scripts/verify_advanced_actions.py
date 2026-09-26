#!/usr/bin/env python3
"""Validate the V2.7 catalog and implemented advanced-action contracts."""

from __future__ import annotations

import json
import hashlib
import pathlib
import sys
import tempfile
from typing import Any, Dict, List


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.advanced_actions import (
    BLUEPRINT_CELL_FIELDS,
    blueprint_cell_path,
    build_cancel_building_blueprint_hint,
    build_cancel_dig_hint,
    build_cancel_utility_blueprint_path_hint,
    build_duplicant_priority_hint,
    build_mark_dig_hint,
    build_place_building_blueprint_hint,
    build_place_utility_blueprint_path_hint,
    dig_cell_path,
    load_catalog,
    utility_path_path,
)
from agent.command_queue import ALLOWED_ACTIONS, _advice_command_is_current_and_valid
from agent.runtime_confirmation import CONFIRMED_MUTATION_MODE
from agent.field_contracts import (
    BLUEPRINT_CATALOG_CONTRACT_ID,
    BLUEPRINT_CELL_CONTRACT_ID,
    BLUEPRINT_INSTANCE_CONTRACT_ID,
    BLUEPRINT_UTILITY_PATH_CONTRACT_ID,
    DIG_CELL_CONTRACT_ID,
    DIG_TOOL_CONTRACT_ID,
    DUPLICANT_CHORE_PRIORITY_CONTRACT_ID,
    GAME_BUILD_MODE_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    contract_quality_fixture,
)
import scripts.verify_runtime as runtime_verifier
from scripts.verify_runtime import (
    BATCH_A_MARKERS,
    BATCH_B_MARKERS,
    BATCH_C_MARKERS,
    blueprint_invalid_target_check,
    blueprint_restore_check,
    dig_designation_check,
    dig_invalid_target_check,
    duplicate_check,
    duplicant_priority_invalid_target_check,
    lineage_session_record_sources,
    observer_check,
    runtime_record_sources,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def attach_confirmed_fixture(command: Dict[str, Any], witness: Dict[str, Any]) -> None:
    """Mark an in-memory scenario fixture as already contract-validated.

    Certificate construction/tamper resistance is exercised separately by
    verify_runtime_confirmation.py; these tests focus on scenario semantics.
    """

    command["status"] = "confirmed"
    command["_confirmation_contract"] = {
        "accepted": True,
        "mode": CONFIRMED_MUTATION_MODE,
        "blockers": [],
        "legacy_read_only": False,
    }
    command["_confirmation_witness"] = json.loads(json.dumps(witness))


SNAPSHOT_TS = "2026-08-30T00:00:00+00:00"


def fixture() -> Dict[str, Any]:
    path = "duplicants.by_instance_id.id_123.chore_priorities.Dig"
    return {
        "schema": "oni.state.v2",
        "ts": SNAPSHOT_TS,
        "game": {"paused": True},
        "duplicants": {
            "by_instance_id": {
                "id_123": {
                    "instance_id": 123,
                    "name": "Ada",
                    "world_id": 0,
                    "chore_priorities": {"Dig": 2, "Build": 3},
                }
            }
        },
        "quality": {
            "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
            path: contract_quality_fixture(DUPLICANT_CHORE_PRIORITY_CONTRACT_ID),
        },
    }


DIG_CELL = 456
DIG_FIELDS = (
    "fingerprint",
    "element_id",
    "mass_kg",
    "damage",
    "solid",
    "designation_present",
    "designation_instance_id",
    "designation_kind",
    "designation_in_progress",
    "safe_mark_candidate",
)

LADDER_CELL = 500
WIRE_CELLS = (600, 601)
CATALOG_FIELDS = (
    "available",
    "prefab_id",
    "is_utility",
    "cancellable",
    "orientation",
    "facade_id",
    "material_tags",
    "available_mass_kg",
    "materials_available",
)
INSTANCE_FIELDS = (
    "instance_id",
    "cell",
    "prefab_id",
    "orientation",
    "material_tags",
    "in_progress",
    "is_utility",
)
PATH_FIELDS = (
    "path_id",
    "building_def_id",
    "cells",
    "blueprint_instance_ids",
    "active_blueprint_instance_ids",
    "active",
    "fully_active",
)


def dig_fixture(*, designated: bool = False) -> Dict[str, Any]:
    prefix = dig_cell_path(DIG_CELL)
    designation_id = 9001 if designated else 0
    details = {
        "cell": DIG_CELL,
        "x": 12,
        "y": 34,
        "world_id": 0,
        "visible": True,
        "solid": True,
        "foundation": False,
        "element_id": "SandStone",
        "mass_kg": 1840.0,
        "hardness": 10,
        "damage": 0.0,
        "has_constructable": False,
        "designation_present": designated,
        "designation_instance_id": designation_id,
        "designation_kind": "tile" if designated else "none",
        "designation_in_progress": False,
        "safe_mark_candidate": not designated,
        "fingerprint": "sha256:" + ("b" if designated else "a") * 64,
    }
    quality = {
        "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
        "designations.dig.tool.tiles_filter_on": contract_quality_fixture(DIG_TOOL_CONTRACT_ID),
        "designations.dig.tool.natural_backwall_filter_on": contract_quality_fixture(DIG_TOOL_CONTRACT_ID),
    }
    for field in DIG_FIELDS:
        quality[f"{prefix}.{field}"] = contract_quality_fixture(DIG_CELL_CONTRACT_ID)
    return {
        "schema": "oni.state.v2",
        "ts": SNAPSHOT_TS,
        "game": {"paused": True},
        "designations": {
            "dig": {
                "tool": {
                    "available": True,
                    "tiles_filter_on": True,
                    "natural_backwall_filter_on": False,
                },
                "candidate_cells": [] if designated else [DIG_CELL],
                "by_cell": {f"cell_{DIG_CELL}": details},
            }
        },
        "quality": quality,
    }


def blueprint_fixture(
    *,
    ladder_placed: bool = False,
    wire_placed: bool = False,
    materials_available: bool = True,
) -> Dict[str, Any]:
    quality: Dict[str, Any] = {
        "game.paused": contract_quality_fixture(GAME_PAUSED_CONTRACT_ID),
        "game.sandbox_mode": contract_quality_fixture(GAME_BUILD_MODE_CONTRACT_ID),
        "game.instant_build_mode": contract_quality_fixture(GAME_BUILD_MODE_CONTRACT_ID),
    }
    catalog = {
        "Ladder": {
            "available": True,
            "prefab_id": "Ladder",
            "is_utility": False,
            "cancellable": True,
            "width": 1,
            "height": 1,
            "build_location_rule": "Anywhere",
            "orientation": "Neutral",
            "facade_id": "DEFAULT_FACADE",
            "material_categories": ["BuildableRaw"],
            "material_tags": ["SandStone"],
            "required_mass_kg": [100.0],
            "available_mass_kg": [1000.0 if materials_available else 0.0],
            "materials_available": materials_available,
            "candidate_cells": [LADDER_CELL],
        },
        "Wire": {
            "available": True,
            "prefab_id": "Wire",
            "is_utility": True,
            "cancellable": True,
            "width": 1,
            "height": 1,
            "build_location_rule": "Anywhere",
            "orientation": "Neutral",
            "facade_id": "DEFAULT_FACADE",
            "material_categories": ["AnyBuildableMetal"],
            "material_tags": ["Copper"],
            "required_mass_kg": [25.0],
            "available_mass_kg": [1000.0 if materials_available else 0.0],
            "materials_available": materials_available,
            "candidate_cells": list(WIRE_CELLS),
        },
    }
    for def_id in catalog:
        for field in CATALOG_FIELDS:
            quality[f"blueprints.catalog.{def_id}.{field}"] = contract_quality_fixture(
                BLUEPRINT_CATALOG_CONTRACT_ID
            )

    def cell_details(
        cell: int,
        x: int,
        *,
        prefab_id: str = "none",
        instance_id: int = 0,
        material_tags: List[str] | None = None,
        is_utility: bool = False,
    ) -> Dict[str, Any]:
        placed = instance_id > 0
        empty_fingerprint = "sha256:" + f"{cell:064x}"[-64:]
        return {
            "cell": cell,
            "x": x,
            "y": 10,
            "world_id": 0,
            "visible": True,
            "solid": False,
            "foundation": False,
            "element_id": "Oxygen",
            "mass_kg": 0.1,
            "damage": 0.0,
            "object_count": 1 if placed else 0,
            "empty_for_blueprint": not placed,
            "can_place_ladder": not placed,
            "can_place_wire": not placed,
            "blueprint_present": placed,
            "blueprint_instance_id": instance_id,
            "blueprint_prefab_id": prefab_id,
            "blueprint_orientation": "Neutral" if placed else "none",
            "blueprint_material_tags": list(material_tags or []),
            "blueprint_in_progress": False,
            "blueprint_is_utility": is_utility if placed else False,
            "empty_fingerprint": empty_fingerprint,
            "fingerprint": ("sha256:" + "f" * 64) if placed else empty_fingerprint,
        }

    by_cell = {
        f"cell_{LADDER_CELL}": cell_details(
            LADDER_CELL,
            10,
            prefab_id="Ladder" if ladder_placed else "none",
            instance_id=9101 if ladder_placed else 0,
            material_tags=["SandStone"] if ladder_placed else [],
        ),
        f"cell_{WIRE_CELLS[0]}": cell_details(
            WIRE_CELLS[0],
            20,
            prefab_id="Wire" if wire_placed else "none",
            instance_id=9201 if wire_placed else 0,
            material_tags=["Copper"] if wire_placed else [],
            is_utility=wire_placed,
        ),
        f"cell_{WIRE_CELLS[1]}": cell_details(
            WIRE_CELLS[1],
            21,
            prefab_id="Wire" if wire_placed else "none",
            instance_id=9202 if wire_placed else 0,
            material_tags=["Copper"] if wire_placed else [],
            is_utility=wire_placed,
        ),
    }
    for cell in (LADDER_CELL, *WIRE_CELLS):
        for field in BLUEPRINT_CELL_FIELDS:
            quality[f"{blueprint_cell_path(cell)}.{field}"] = contract_quality_fixture(
                BLUEPRINT_CELL_CONTRACT_ID
            )

    by_instance: Dict[str, Any] = {}
    for cell in (LADDER_CELL, *WIRE_CELLS):
        details = by_cell[f"cell_{cell}"]
        instance_id = details["blueprint_instance_id"]
        if instance_id <= 0:
            continue
        identity = {
            "instance_id": instance_id,
            "cell": cell,
            "world_id": 0,
            "prefab_id": details["blueprint_prefab_id"],
            "orientation": details["blueprint_orientation"],
            "material_tags": details["blueprint_material_tags"],
            "in_progress": False,
            "is_utility": details["blueprint_is_utility"],
            "fingerprint": details["fingerprint"],
        }
        key = f"id_{instance_id}"
        by_instance[key] = identity
        for field in INSTANCE_FIELDS:
            quality[f"blueprints.by_instance_id.{key}.{field}"] = contract_quality_fixture(
                BLUEPRINT_INSTANCE_CONTRACT_ID
            )

    utility_paths: Dict[str, Any] = {}
    if wire_placed:
        path_id = "wirepath1"
        ids = [9201, 9202]
        utility_paths[f"id_{path_id}"] = {
            "path_id": path_id,
            "building_def_id": "Wire",
            "cells": list(WIRE_CELLS),
            "blueprint_instance_ids": ids,
            "active_blueprint_instance_ids": ids,
            "active": True,
            "fully_active": True,
        }
        for field in PATH_FIELDS:
            quality[f"{utility_path_path(path_id)}.{field}"] = contract_quality_fixture(
                BLUEPRINT_UTILITY_PATH_CONTRACT_ID
            )

    return {
        "schema": "oni.state.v2",
        "ts": SNAPSHOT_TS,
        "game": {"paused": True, "sandbox_mode": False, "instant_build_mode": False},
        "blueprints": {
            "catalog": catalog,
            "by_instance_id": by_instance,
            "utility_paths": utility_paths,
        },
        "cells": {"by_id": by_cell},
        "quality": quality,
    }


def check_catalog() -> Dict[str, Any]:
    catalog = load_catalog()
    actions = catalog.get("actions")
    require(catalog.get("catalog_complete") is True, "catalog is not frozen as complete")
    require(catalog.get("batch_order") == ["A", "B", "C", "D"], "batch order changed")
    require(set(catalog.get("batch_blockers", {})) == {"A", "B", "C", "D"}, "batch blockers are incomplete")
    require(isinstance(actions, list) and len(actions) == 13, "expected 13 V2.7 actions")
    ids = [str(item.get("id")) for item in actions if isinstance(item, dict)]
    require(len(ids) == len(set(ids)), "advanced action ids are not unique")
    required_fields = {
        "id",
        "batch",
        "plane",
        "level",
        "risk",
        "status",
        "max_targets",
        "summary",
        "required_state",
        "rollback",
        "api_evidence",
    }
    for item in actions:
        require(isinstance(item, dict), "action row is not an object")
        require(not (required_fields - set(item)), f"action row is incomplete: {item.get('id')}")
        require(item["batch"] in catalog["batch_order"], f"unknown batch: {item['batch']}")
        require(item["plane"] in {"game", "bridge"}, f"unknown execution plane: {item['id']}")
        require(item["level"] in {"L2", "L3"}, f"unexpected level: {item['id']}")
        require(item["risk"] in {"low", "medium", "high"}, f"unexpected risk: {item['id']}")
        require(item["status"] in {"planned", "implementing", "verified"}, f"unexpected status: {item['id']}")
        require(isinstance(item["max_targets"], int) and item["max_targets"] > 0, f"invalid max_targets: {item['id']}")
    forbidden = catalog.get("permanently_forbidden")
    require(isinstance(forbidden, list) and len(forbidden) == 8, "permanent forbidden list is incomplete")
    require("instant_build_or_direct_building_complete_creation" in forbidden, "instant build is not forbidden")
    serialized = json.dumps(catalog, ensure_ascii=False)
    require("BuildingDef.Build(" not in serialized, "catalog still names direct completed-building API")
    require("BuildingDef.TryPlace(" in serialized, "catalog is missing the blueprint placement API")
    batch_a = next(item for item in actions if item.get("id") == "set_duplicant_chore_priority")
    require(batch_a.get("status") == "verified", "batch A is not marked verified")
    batch_b = [item for item in actions if item.get("batch") == "B"]
    require(
        len(batch_b) == 2 and all(item.get("status") in {"implementing", "verified"} for item in batch_b),
        "batch B is not marked implementing or verified",
    )
    batch_c = [item for item in actions if item.get("batch") == "C"]
    require(
        len(batch_c) == 4 and all(item.get("status") == "verified" for item in batch_c),
        "batch C is not fully verified",
    )
    batch_d = [item for item in actions if item.get("batch") == "D"]
    require(len(batch_d) == 6 and all(item.get("status") == "verified" for item in batch_d), "batch D is not fully verified")
    require(not catalog.get("batch_blockers", {}).get("C"), "batch C still has blockers")
    require(not catalog.get("batch_blockers", {}).get("D"), "batch D still has blockers")
    return {"actions": len(actions), "forbidden": len(forbidden), "batches": catalog["batch_order"]}


def check_batch_a_contract() -> Dict[str, Any]:
    hint = build_duplicant_priority_hint(fixture(), instance_id=123, chore_group="Dig", priority=4)
    require(hint["action"] == "set_duplicant_chore_priority", "wrong batch-A action")
    require(hint["args"]["from_priority"] == 2 and hint["args"]["priority"] == 4, "priority binding failed")
    require(len(hint["preconditions"]) == 2, "paused/current-value preconditions are required")
    require(hint["expected_effects"][0]["value"] == 4, "target postcondition is missing")
    require(
        {item.get("quality_contract_id") for item in hint["preconditions"] + hint["expected_effects"]}
        == {GAME_PAUSED_CONTRACT_ID, DUPLICANT_CHORE_PRIORITY_CONTRACT_ID},
        "batch-A conditions are not bound to exact V3 quality contracts",
    )
    require("set_duplicant_chore_priority" in ALLOWED_ACTIONS, "command queue does not allow batch A")

    rejected: List[str] = []
    invalid_cases = [
        ({**fixture(), "game": {"paused": False}}, 4, "not_paused"),
        (fixture(), 2, "no_change"),
        (fixture(), 6, "out_of_range"),
    ]
    for state, target, label in invalid_cases:
        try:
            build_duplicant_priority_hint(state, instance_id=123, chore_group="Dig", priority=target)
        except ValueError:
            rejected.append(label)
    require(len(rejected) == len(invalid_cases), "batch-A invalid cases were not all rejected")

    for invalid_group in ("", "Dig.Group"):
        try:
            build_duplicant_priority_hint(fixture(), instance_id=123, chore_group=invalid_group, priority=4)
        except ValueError:
            rejected.append("invalid_group")
    require(rejected.count("invalid_group") == 2, "unsafe chore-group path segments were not rejected")

    tampered = fixture()
    priority_path = "duplicants.by_instance_id.id_123.chore_priorities.Dig"
    tampered["quality"][priority_path]["semantic"] = "current_priority_guess"
    try:
        build_duplicant_priority_hint(tampered, instance_id=123, chore_group="Dig", priority=4)
    except ValueError:
        rejected.append("tampered_priority_metadata")
    require("tampered_priority_metadata" in rejected, "tampered priority metadata authorized a hint")

    executor = (PROJECT_ROOT / "mod" / "Source" / "Commands" / "ActionExecutor.cs").read_text(encoding="utf-8")
    reader = (PROJECT_ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs").read_text(encoding="utf-8")
    policy = (PROJECT_ROOT / "scripts" / "write-action-policy.ps1").read_text(encoding="utf-8")
    required_executor = (
        'case "set_duplicant_chore_priority"',
        "ChoreConsumer.MIN_PERSONAL_PRIORITY",
        "consumer.SetPersonalPriority(group, priority)",
        "ContainsObservedEquality(preconditions, \"game.paused\"",
        "ProbeSession.CurrentWorldId",
        "groupId.All(char.IsLetterOrDigit)",
    )
    require(all(fragment in executor for fragment in required_executor), "executor batch-A static contract is incomplete")
    require("GetPersonalPriority(group)" in reader and '"by_instance_id"' in reader, "probe batch-A state is incomplete")
    require("set_duplicant_chore_priority" in policy, "policy writer does not expose batch A")
    return {"action": hint["action"], "rejected_cases": rejected, "typed_executor": True}


def check_batch_a_runtime_contract() -> Dict[str, Any]:
    command_id = "cmd-runtime-priority-invalid-fixture"
    command = {
        "id": command_id,
        "action": "set_duplicant_chore_priority",
        "status": "quarantined",
        "_directory": "quarantine",
        "ack_status": "rejected",
        "classification": "behavior_fail",
        "quarantine_reason": "Duplicant instance id matched but expected_name did not.",
        "args": {
            "duplicant_instance_id": 123,
            "expected_name": "Ada__invalid_target__",
            "chore_group": "Dig",
            "from_priority": 2,
            "priority": 4,
        },
    }
    records = [{"event": "command_ack", "id": command_id, "status": "rejected"}]
    result = duplicant_priority_invalid_target_check([command], records, [fixture()])
    require(result["status"] == "passed", "runtime invalid-target rejection contract did not pass")

    executed = records + [{"event": "command_ack", "id": command_id, "status": "executed"}]
    unsafe = duplicant_priority_invalid_target_check([command], executed, [fixture()])
    require(unsafe["status"] == "failed", "runtime invalid-target contract accepted an execution ack")
    require(
        BATCH_A_MARKERS == (
            "observer_start",
            "duplicant_priority_runtime",
            "duplicant_priority_invalid_target",
        ),
        "batch-A runtime marker set changed",
    )
    return {"expected_rejection": result["status"], "execution_ack_rejected": unsafe["status"] == "failed"}


def check_batch_b_contract() -> Dict[str, Any]:
    mark = build_mark_dig_hint(dig_fixture(), cells=[DIG_CELL])
    require(mark["action"] == "mark_dig_cells", "wrong batch-B mark action")
    require(mark["args"] == {"cells": [DIG_CELL]}, "mark cells were not bound exactly")
    require(
        all(item.get("quality_contract_id") for item in mark["preconditions"] + mark["expected_effects"]),
        "batch-B conditions are missing V3 quality contract ids",
    )
    require(
        any(item.get("path", "").endswith(".designation_instance_id") and item.get("op") == "gt" for item in mark["expected_effects"]),
        "mark contract does not require a created designation instance",
    )

    cancel = build_cancel_dig_hint(dig_fixture(designated=True), cells=[DIG_CELL])
    require(cancel["action"] == "cancel_dig_cells", "wrong batch-B cancel action")
    require(
        cancel["args"] == {"targets": [{"cell": DIG_CELL, "designation_instance_id": 9001}]},
        "cancel target is not bound to the exact designation instance",
    )
    require(
        {"mark_dig_cells", "cancel_dig_cells"}.issubset(ALLOWED_ACTIONS),
        "command queue does not allow both batch-B actions",
    )

    rejected: List[str] = []
    invalid_mark_states = []
    not_paused = dig_fixture()
    not_paused["game"]["paused"] = False
    invalid_mark_states.append((not_paused, [DIG_CELL], "not_paused"))
    tiles_off = dig_fixture()
    tiles_off["designations"]["dig"]["tool"]["tiles_filter_on"] = False
    invalid_mark_states.append((tiles_off, [DIG_CELL], "tiles_off"))
    backwall_on = dig_fixture()
    backwall_on["designations"]["dig"]["tool"]["natural_backwall_filter_on"] = True
    invalid_mark_states.append((backwall_on, [DIG_CELL], "backwall_on"))
    unsafe = dig_fixture()
    unsafe["designations"]["dig"]["by_cell"][f"cell_{DIG_CELL}"]["safe_mark_candidate"] = False
    invalid_mark_states.append((unsafe, [DIG_CELL], "unsafe_cell"))
    invalid_mark_states.append((dig_fixture(), [DIG_CELL, DIG_CELL], "duplicate_cell"))
    for state, cells, label in invalid_mark_states:
        try:
            build_mark_dig_hint(state, cells=cells)
        except ValueError:
            rejected.append(label)

    wrong_kind = dig_fixture(designated=True)
    wrong_kind["designations"]["dig"]["by_cell"][f"cell_{DIG_CELL}"]["designation_kind"] = "backwall"
    in_progress = dig_fixture(designated=True)
    in_progress["designations"]["dig"]["by_cell"][f"cell_{DIG_CELL}"]["designation_in_progress"] = True
    missing = dig_fixture()
    for state, label in ((wrong_kind, "wrong_kind"), (in_progress, "in_progress"), (missing, "missing_designation")):
        try:
            build_cancel_dig_hint(state, cells=[DIG_CELL])
        except ValueError:
            rejected.append(label)
    require(
        set(rejected)
        == {
            "not_paused",
            "tiles_off",
            "backwall_on",
            "unsafe_cell",
            "duplicate_cell",
            "wrong_kind",
            "in_progress",
            "missing_designation",
        },
        "batch-B invalid cases were not all rejected",
    )

    tampered = dig_fixture()
    tampered["quality"][f"{dig_cell_path(DIG_CELL)}.fingerprint"]["source"] = "fixture"
    try:
        build_mark_dig_hint(tampered, cells=[DIG_CELL])
    except ValueError:
        rejected.append("tampered_dig_metadata")
    require("tampered_dig_metadata" in rejected, "tampered dig metadata authorized a hint")

    executor = (PROJECT_ROOT / "mod" / "Source" / "Commands" / "ActionExecutor.cs").read_text(encoding="utf-8")
    reader = (PROJECT_ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs").read_text(encoding="utf-8")
    policy = (PROJECT_ROOT / "scripts" / "write-action-policy.ps1").read_text(encoding="utf-8")
    required_executor = (
        'case "mark_dig_cells"',
        'case "cancel_dig_cells"',
        "DigTool.PlaceDig(cell, created.Count)",
        "designation.gameObject.Trigger(CancelEventHash)",
        "prefab.InstanceID != target.DesignationInstanceId",
        "designation.chore.InProgress()",
        "ValidateDigConditionContract",
    )
    required_reader = (
        'Timed("designations", ReadDesignations, metrics)',
        "digTool.IsActiveLayer",
        "Components.Diggables.GetWorldItems",
        "IsSafeDigMarkCandidate",
        "DigCellFingerprint",
        '"designation_instance_id"',
    )
    require(all(fragment in executor for fragment in required_executor), "executor batch-B static contract is incomplete")
    require(all(fragment in reader for fragment in required_reader), "probe batch-B state is incomplete")
    require("mark_dig_cells" in policy and "cancel_dig_cells" in policy, "policy writer does not expose batch B")
    require("SimMessages.Dig" not in executor and "WorldDamage" not in executor, "executor contains instant-dig APIs")
    place_index = executor.index("GameObject gameObject = DigTool.PlaceDig(cell, created.Count)")
    track_index = executor.index("created.Add(gameObject);", place_index)
    component_check_index = executor.index("gameObject.GetComponent<Diggable>() == null", place_index)
    require(
        place_index < track_index < component_check_index,
        "PlaceDig results must enter the rollback set before synchronous component checks",
    )
    return {
        "actions": [mark["action"], cancel["action"]],
        "exact_designation_id": cancel["args"]["targets"][0]["designation_instance_id"],
        "rejected_cases": sorted(rejected),
        "typed_executor": True,
    }


def check_batch_b_runtime_contract() -> Dict[str, Any]:
    session_id = "session-dig-fixture"
    mark_id = "cmd-runtime-dig-mark-fixture"
    cancel_id = "cmd-runtime-dig-cancel-fixture"
    invalid_id = "cmd-runtime-dig-invalid-fixture"
    before = dig_fixture()
    marked = dig_fixture(designated=True)
    commands = [
        {
            "id": mark_id,
            "action": "mark_dig_cells",
            "session_id": session_id,
            "status": "verified",
            "_directory": "done",
            "completed_at": "2026-07-11T00:00:01+08:00",
            "args": {"cells": [DIG_CELL]},
            "result": {"before_state": before, "after_state": marked},
        },
        {
            "id": cancel_id,
            "action": "cancel_dig_cells",
            "session_id": session_id,
            "status": "verified",
            "_directory": "done",
            "completed_at": "2026-07-11T00:00:02+08:00",
            "args": {"targets": [{"cell": DIG_CELL, "designation_instance_id": 9001}]},
            "result": {"before_state": marked, "after_state": before},
        },
        {
            "id": invalid_id,
            "action": "mark_dig_cells",
            "session_id": session_id,
            "status": "quarantined",
            "_directory": "quarantine",
            "ack_status": "rejected",
            "classification": "behavior_fail",
            "quarantine_reason": "Current-state precondition failed: fingerprint mismatch",
            "evidence": {
                "cell": DIG_CELL,
                "expected_unchanged_fingerprint": "sha256:" + "a" * 64,
            },
        },
    ]
    records = []
    attach_confirmed_fixture(commands[0], marked)
    attach_confirmed_fixture(commands[1], before)
    for command_id in (mark_id, cancel_id):
        records.extend(
            {"event": "command_ack", "id": command_id, "status": status}
            for status in ("accepted", "executed", "local_effect_observed")
        )
    records.append({"event": "command_ack", "id": invalid_id, "status": "rejected"})

    pair = dig_designation_check(commands, records, session_id)
    require(pair["status"] == "passed" and len(pair["restore_pairs"]) == 1, "mark/cancel restore contract failed")
    invalid = dig_invalid_target_check(commands, records, [before], session_id)
    require(invalid["status"] == "passed", "invalid dig fingerprint rejection contract failed")

    unsafe_records = records + [{"event": "command_ack", "id": invalid_id, "status": "executed"}]
    unsafe = dig_invalid_target_check(commands, unsafe_records, [before], session_id)
    require(unsafe["status"] == "failed", "invalid dig contract accepted an execution ack")
    require(
        BATCH_B_MARKERS == ("observer_start", "dig_designation_runtime", "dig_designation_invalid_target"),
        "batch-B runtime marker set changed",
    )
    return {
        "restore_pair": pair["restore_pairs"][0],
        "expected_rejection": invalid["status"],
        "execution_ack_rejected": unsafe["status"] == "failed",
    }


def check_batch_c_contract() -> Dict[str, Any]:
    empty = blueprint_fixture()
    ladder_place = build_place_building_blueprint_hint(
        empty,
        cells=[LADDER_CELL],
        material_tags=["SandStone"],
    )
    require(ladder_place["action"] == "place_building_blueprint", "wrong Ladder placement action")
    require(
        ladder_place["args"]
        == {
            "building_def_id": "Ladder",
            "cells": [LADDER_CELL],
            "orientation": "Neutral",
            "material_tags": ["SandStone"],
            "facade_id": "DEFAULT_FACADE",
        },
        "Ladder placement args are not exact",
    )
    require(
        any(
            item.get("path", "").endswith(".blueprint_instance_id") and item.get("op") == "gt"
            for item in ladder_place["expected_effects"]
        ),
        "Ladder placement does not require a created instance id",
    )

    ladder_cancel = build_cancel_building_blueprint_hint(
        blueprint_fixture(ladder_placed=True),
        cells=[LADDER_CELL],
    )
    require(
        ladder_cancel["args"]
        == {"targets": [{"cell": LADDER_CELL, "blueprint_instance_id": 9101}]},
        "Ladder cancellation is not bound to the exact instance",
    )
    require(
        any(
            item.get("path") == f"{blueprint_cell_path(LADDER_CELL)}.fingerprint"
            and item.get("value")
            == blueprint_fixture(ladder_placed=True)["cells"]["by_id"][f"cell_{LADDER_CELL}"][
                "empty_fingerprint"
            ]
            for item in ladder_cancel["expected_effects"]
        ),
        "Ladder cancellation does not require restoration fingerprint",
    )

    wire_place = build_place_utility_blueprint_path_hint(
        empty,
        path_id="wirepath1",
        cells=WIRE_CELLS,
        material_tags=["Copper"],
    )
    require(wire_place["action"] == "place_utility_blueprint_path", "wrong Wire placement action")
    require(wire_place["args"]["cells"] == list(WIRE_CELLS), "Wire path order changed")
    require(
        all(
            item.get("quality_contract_id")
            for item in wire_place["preconditions"] + wire_place["expected_effects"]
        ),
        "batch-C conditions are missing V3 quality contract ids",
    )
    require(
        any(item.get("path", "").endswith(".fully_active") and item.get("value") is True for item in wire_place["expected_effects"]),
        "Wire placement lacks a fully-active path postcondition",
    )
    empty["snapshot_id"] = "session-blueprint-fixture:1"
    require(
        _advice_command_is_current_and_valid(
            empty,
            {"source_snapshot_id": empty["snapshot_id"], "command": wire_place},
        ),
        "command queue rejected a valid utility hint whose future path does not yet exist",
    )

    shortage = blueprint_fixture(materials_available=False)
    shortage_hints = (
        build_place_building_blueprint_hint(
            shortage,
            cells=[LADDER_CELL],
            material_tags=["SandStone"],
        ),
        build_place_utility_blueprint_path_hint(
            shortage,
            path_id="wirepathshortage",
            cells=WIRE_CELLS,
            material_tags=["Copper"],
        ),
    )
    require(
        all(
            not any(item.get("path", "").endswith(".materials_available") for item in hint["preconditions"])
            for hint in shortage_hints
        ),
        "blueprint placement still treats current inventory as a creation precondition",
    )

    wire_cancel = build_cancel_utility_blueprint_path_hint(
        blueprint_fixture(wire_placed=True),
        path_id="wirepath1",
    )
    require(wire_cancel["action"] == "cancel_utility_blueprint_path", "wrong Wire cancel action")
    require(
        wire_cancel["args"]
        == {
            "path_id": "wirepath1",
            "targets": [
                {"cell": WIRE_CELLS[0], "blueprint_instance_id": 9201},
                {"cell": WIRE_CELLS[1], "blueprint_instance_id": 9202},
            ],
        },
        "Wire cancellation is not bound to the exact path instances",
    )
    require(
        {
            "place_building_blueprint",
            "cancel_building_blueprint",
            "place_utility_blueprint_path",
            "cancel_utility_blueprint_path",
        }.issubset(ALLOWED_ACTIONS),
        "command queue does not allow all batch-C actions",
    )

    rejected: List[str] = []
    sandbox = blueprint_fixture()
    sandbox["game"]["sandbox_mode"] = True
    instant = blueprint_fixture()
    instant["game"]["instant_build_mode"] = True
    noncontiguous = blueprint_fixture()
    noncontiguous["cells"]["by_id"][f"cell_{WIRE_CELLS[1]}"]["x"] = 23
    in_progress = blueprint_fixture(ladder_placed=True)
    in_progress["cells"]["by_id"][f"cell_{LADDER_CELL}"]["blueprint_in_progress"] = True
    in_progress["blueprints"]["by_instance_id"]["id_9101"]["in_progress"] = True
    bad_path = blueprint_fixture(wire_placed=True)
    bad_path["blueprints"]["utility_paths"]["id_wirepath1"]["active_blueprint_instance_ids"] = [9202, 9201]
    invalid_calls = (
        (lambda: build_place_building_blueprint_hint(sandbox, cells=[LADDER_CELL], material_tags=["SandStone"]), "sandbox"),
        (lambda: build_place_building_blueprint_hint(instant, cells=[LADDER_CELL], material_tags=["SandStone"]), "instant_build"),
        (lambda: build_place_building_blueprint_hint(empty, cells=[LADDER_CELL], material_tags=["Copper"]), "wrong_material"),
        (
            lambda: build_place_utility_blueprint_path_hint(
                noncontiguous,
                path_id="wirepath1",
                cells=WIRE_CELLS,
                material_tags=["Copper"],
            ),
            "noncontiguous_path",
        ),
        (lambda: build_cancel_building_blueprint_hint(in_progress, cells=[LADDER_CELL]), "in_progress"),
        (lambda: build_cancel_utility_blueprint_path_hint(bad_path, path_id="wirepath1"), "path_instance_mismatch"),
        (
            lambda: build_place_utility_blueprint_path_hint(
                empty,
                path_id="wire-path",
                cells=WIRE_CELLS,
                material_tags=["Copper"],
            ),
            "unsafe_path_id",
        ),
    )
    for callback, label in invalid_calls:
        try:
            callback()
        except ValueError:
            rejected.append(label)
    require(
        set(rejected)
        == {
            "sandbox",
            "instant_build",
            "wrong_material",
            "noncontiguous_path",
            "in_progress",
            "path_instance_mismatch",
            "unsafe_path_id",
        },
        "batch-C invalid cases were not all rejected",
    )

    tampered = blueprint_fixture()
    tampered["quality"][f"{blueprint_cell_path(LADDER_CELL)}.fingerprint"]["scope"] = "active_world"
    try:
        build_place_building_blueprint_hint(
            tampered,
            cells=[LADDER_CELL],
            material_tags=["SandStone"],
        )
    except ValueError:
        rejected.append("tampered_blueprint_metadata")
    require("tampered_blueprint_metadata" in rejected, "tampered blueprint metadata authorized a hint")

    executor = (PROJECT_ROOT / "mod" / "Source" / "Commands" / "ActionExecutor.cs").read_text(encoding="utf-8")
    reader = (PROJECT_ROOT / "mod" / "Source" / "Probe" / "ProbeStateReader.cs").read_text(encoding="utf-8")
    policy = (PROJECT_ROOT / "scripts" / "write-action-policy.ps1").read_text(encoding="utf-8")
    required_executor = (
        'case "place_building_blueprint"',
        'case "cancel_building_blueprint"',
        'case "place_utility_blueprint_path"',
        'case "cancel_utility_blueprint_path"',
        "def.TryPlace(",
        "ValidateBlueprintConditionContract",
        "ProbeStateReader.UtilityPathMatches",
        "TryReadBlueprintConstructionState(constructable",
        "CancelCreatedBlueprints(pending.CreatedBlueprints)",
    )
    required_reader = (
        'Timed("blueprints", ReadBlueprintState, metrics)',
        '"sandbox_mode"',
        '"instant_build_mode"',
        '"empty_fingerprint"',
        "BuildingDef+Grid+Constructable+WorldInventory",
        "RegisterUtilityPath",
    )
    require(all(fragment in executor for fragment in required_executor), "executor batch-C static contract is incomplete")
    require(all(fragment in reader for fragment in required_reader), "probe batch-C state is incomplete")
    require("def.Build(" not in executor, "executor contains direct completed-building creation")
    require(
        "Selected blueprint material inventory is insufficient" not in executor
        and 'catalogPrefix + ".materials_available"' not in executor
        and "!def.MaterialsAvailable(selectedMaterials, world)" not in reader,
        "blueprint creation still requires current material inventory",
    )
    place_index = executor.index("GameObject blueprint = def.TryPlace(")
    track_index = executor.index("created.Add(blueprint);", place_index)
    component_index = executor.index("blueprint.GetComponent<BuildingComplete>()", place_index)
    require(
        place_index < track_index < component_index,
        "TryPlace results must enter the rollback set before synchronous component checks",
    )
    require(
        all(action in policy for action in ALLOWED_ACTIONS if "blueprint" in action),
        "policy writer does not expose every batch-C action",
    )
    return {
        "actions": [
            ladder_place["action"],
            ladder_cancel["action"],
            wire_place["action"],
            wire_cancel["action"],
        ],
        "rejected_cases": sorted(rejected),
        "tryplace_only": True,
        "automatic_place_rollback": True,
        "shortage_blueprints_supported": True,
    }


def check_batch_c_runtime_contract() -> Dict[str, Any]:
    session_id = "session-blueprint-fixture"
    empty = blueprint_fixture(materials_available=False)
    ladder = blueprint_fixture(ladder_placed=True, materials_available=False)
    wire = blueprint_fixture(wire_placed=True, materials_available=False)
    wire_cancelled = json.loads(json.dumps(empty))
    wire_cancelled["blueprints"]["utility_paths"] = json.loads(
        json.dumps(wire["blueprints"]["utility_paths"])
    )
    wire_path = wire_cancelled["blueprints"]["utility_paths"]["id_wirepath1"]
    wire_path["active_blueprint_instance_ids"] = []
    wire_path["active"] = False
    wire_path["fully_active"] = False
    for field in PATH_FIELDS:
        path = f"{utility_path_path('wirepath1')}.{field}"
        wire_cancelled["quality"][path] = contract_quality_fixture(
            BLUEPRINT_UTILITY_PATH_CONTRACT_ID
        )
    commands = [
        {
            "id": "cmd-runtime-blueprint-place-fixture",
            "action": "place_building_blueprint",
            "session_id": session_id,
            "status": "verified",
            "_directory": "done",
            "completed_at": "2026-07-11T00:00:01+08:00",
            "args": {
                "building_def_id": "Ladder",
                "cells": [LADDER_CELL],
                "orientation": "Neutral",
                "material_tags": ["SandStone"],
                "facade_id": "DEFAULT_FACADE",
            },
            "result": {"before_state": empty, "after_state": ladder},
        },
        {
            "id": "cmd-runtime-blueprint-cancel-fixture",
            "action": "cancel_building_blueprint",
            "session_id": session_id,
            "status": "verified",
            "_directory": "done",
            "completed_at": "2026-07-11T00:00:02+08:00",
            "args": {"targets": [{"cell": LADDER_CELL, "blueprint_instance_id": 9101}]},
            "result": {"before_state": ladder, "after_state": empty},
        },
        {
            "id": "cmd-runtime-utility-place-fixture",
            "action": "place_utility_blueprint_path",
            "session_id": session_id,
            "status": "verified",
            "_directory": "done",
            "completed_at": "2026-07-11T00:00:03+08:00",
            "args": {
                "path_id": "wirepath1",
                "building_def_id": "Wire",
                "cells": list(WIRE_CELLS),
                "orientation": "Neutral",
                "material_tags": ["Copper"],
                "facade_id": "DEFAULT_FACADE",
            },
            "result": {"before_state": empty, "after_state": wire},
        },
        {
            "id": "cmd-runtime-utility-cancel-fixture",
            "action": "cancel_utility_blueprint_path",
            "session_id": session_id,
            "status": "verified",
            "_directory": "done",
            "completed_at": "2026-07-11T00:00:04+08:00",
            "args": {
                "path_id": "wirepath1",
                "targets": [
                    {"cell": WIRE_CELLS[0], "blueprint_instance_id": 9201},
                    {"cell": WIRE_CELLS[1], "blueprint_instance_id": 9202},
                ],
            },
            "result": {"before_state": wire, "after_state": wire_cancelled},
        },
        {
            "id": "cmd-runtime-blueprint-invalid-fixture",
            "action": "place_building_blueprint",
            "session_id": session_id,
            "status": "quarantined",
            "_directory": "quarantine",
            "ack_status": "rejected",
            "classification": "behavior_fail",
            "quarantine_reason": "Blueprint placement must bind matching observed empty and current fingerprints for every cell.",
            "evidence": {
                "cell": LADDER_CELL,
                "expected_unchanged_fingerprint": empty["cells"]["by_id"][f"cell_{LADDER_CELL}"]["fingerprint"],
            },
        },
    ]
    records: List[Dict[str, Any]] = []
    attach_confirmed_fixture(commands[0], ladder)
    attach_confirmed_fixture(commands[1], empty)
    attach_confirmed_fixture(commands[2], wire)
    attach_confirmed_fixture(commands[3], wire_cancelled)
    for command_id in (
        "cmd-runtime-blueprint-place-fixture",
        "cmd-runtime-blueprint-cancel-fixture",
        "cmd-runtime-utility-place-fixture",
        "cmd-runtime-utility-cancel-fixture",
    ):
        records.extend(
            {"event": "command_ack", "id": command_id, "status": status}
            for status in ("accepted", "executed", "local_effect_observed")
        )
    records.append(
        {"event": "command_ack", "id": "cmd-runtime-blueprint-invalid-fixture", "status": "rejected"}
    )
    building = blueprint_restore_check(
        commands,
        records,
        place_action="place_building_blueprint",
        cancel_action="cancel_building_blueprint",
        place_prefix="cmd-runtime-blueprint-place-",
        cancel_prefix="cmd-runtime-blueprint-cancel-",
        def_id="Ladder",
        is_utility=False,
        session_id=session_id,
    )
    utility = blueprint_restore_check(
        commands,
        records,
        place_action="place_utility_blueprint_path",
        cancel_action="cancel_utility_blueprint_path",
        place_prefix="cmd-runtime-utility-place-",
        cancel_prefix="cmd-runtime-utility-cancel-",
        def_id="Wire",
        is_utility=True,
        session_id=session_id,
    )
    invalid = blueprint_invalid_target_check(commands, records, [empty], session_id)
    require(building["status"] == "passed", "building blueprint runtime restore contract failed")
    require(utility["status"] == "passed", "utility blueprint runtime restore contract failed")
    require(invalid["status"] == "passed", "invalid blueprint target rejection contract failed")
    for result in (building, utility):
        placement = result["placement_evidence"][0]
        require(
            placement["materials_available_before"] is False
            and placement["available_mass_kg_before"] == [0.0]
            and placement["zero_inventory_observed"] is True,
            "blueprint runtime evidence is not explicitly bound to observed zero inventory",
        )

    stocked_commands = json.loads(json.dumps(commands))
    for command in stocked_commands:
        if command.get("action") not in {"place_building_blueprint", "place_utility_blueprint_path"}:
            continue
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        if not args or not result:
            continue
        command_def_id = args["building_def_id"]
        catalog = result["before_state"]["blueprints"]["catalog"][command_def_id]
        catalog["materials_available"] = True
        catalog["available_mass_kg"] = [1000.0]
    stocked_building = blueprint_restore_check(
        stocked_commands,
        records,
        place_action="place_building_blueprint",
        cancel_action="cancel_building_blueprint",
        place_prefix="cmd-runtime-blueprint-place-",
        cancel_prefix="cmd-runtime-blueprint-cancel-",
        def_id="Ladder",
        is_utility=False,
        session_id=session_id,
    )
    stocked_utility = blueprint_restore_check(
        stocked_commands,
        records,
        place_action="place_utility_blueprint_path",
        cancel_action="cancel_utility_blueprint_path",
        place_prefix="cmd-runtime-utility-place-",
        cancel_prefix="cmd-runtime-utility-cancel-",
        def_id="Wire",
        is_utility=True,
        session_id=session_id,
    )
    require(
        stocked_building["status"] == "failed" and stocked_utility["status"] == "failed",
        "stocked blueprint evidence was accepted as the zero-inventory runtime scenario",
    )
    unsafe = blueprint_invalid_target_check(
        commands,
        records
        + [{"event": "command_ack", "id": "cmd-runtime-blueprint-invalid-fixture", "status": "executed"}],
        [empty],
        session_id,
    )
    require(unsafe["status"] == "failed", "invalid blueprint contract accepted an execution ack")
    unrelated_reason_commands = json.loads(json.dumps(commands))
    unrelated_reason_commands[-1]["quarantine_reason"] = "Blueprint fingerprint diagnostics unavailable."
    unrelated_reason = blueprint_invalid_target_check(
        unrelated_reason_commands,
        records,
        [empty],
        session_id,
    )
    require(unrelated_reason["status"] == "failed", "unrelated fingerprint text was accepted as a precondition rejection")
    require(
        BATCH_C_MARKERS
        == (
            "observer_start",
            "building_blueprint_runtime",
            "utility_blueprint_path_runtime",
            "blueprint_invalid_target",
        ),
        "batch-C runtime marker set changed",
    )
    return {
        "building_restore_pair": building["restore_pairs"][0],
        "utility_restore_pair": utility["restore_pairs"][0],
        "expected_rejection": invalid["status"],
        "zero_inventory_bound": True,
        "stocked_evidence_rejected": True,
        "execution_ack_rejected": unsafe["status"] == "failed",
        "unrelated_reason_rejected": unrelated_reason["status"] == "failed",
    }


def check_runtime_rotated_log_contract() -> Dict[str, Any]:
    session_id = "session-rotated-fixture"

    def snapshot(seq: int) -> Dict[str, Any]:
        quality = {
            path: {"status": "observed", "source": "fixture"}
            for path in ("game.paused", "cycle", "duplicants.total", "resources.food_kcal", "research.current")
        }
        return {
            "event": "state_snapshot",
            "schema": "oni.state.v2",
            "session_id": session_id,
            "seq": seq,
            "game_build": "fixture-build",
            "mod_version": "fixture-version",
            "save_id_hash": "sha256:fixture",
            "world_id": 0,
            "snapshot_id": f"snapshot-{seq}",
            "game": {"paused": True},
            "cycle": 7,
            "duplicants": {"total": 3},
            "resources": {"food_kcal": 12000},
            "research": {"current": None},
            "quality": quality,
        }

    def write_jsonl(path: pathlib.Path, records: List[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="oni-runtime-rotation-") as temporary:
        root = pathlib.Path(temporary)
        (root / "latest_state.json").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
        write_jsonl(
            root / "state.jsonl.1",
            [
                {"event": "probe_loaded"},
                {"event": "probe_patch_registered"},
                snapshot(1),
            ],
        )
        write_jsonl(root / "state.jsonl", [snapshot(3)])
        archive_records = [snapshot(1), snapshot(2), snapshot(3)] + [
            {"event": "command_ack", "session_id": session_id, "id": "cmd-fixture", "status": status}
            for status in ("accepted", "executed", "local_effect_observed")
        ]
        write_jsonl(root / "sessions" / session_id / "state.jsonl", archive_records)

        sources = runtime_record_sources(root)
        observer = observer_check(
            sources["session_id"],
            sources["lifecycle_records"],
            sources["session_records"],
            sources["snapshots"],
            sources["invalid_lines"],
            {"exists": True, "passed": True},
        )
        ack_statuses = [
            record.get("status")
            for record in sources["session_records"]
            if record.get("event") == "command_ack" and record.get("id") == "cmd-fixture"
        ]
        require(sources["session_archive_used"], "complete session archive was not selected")
        require(len(sources["snapshots"]) == 3, "rotated root tail replaced the complete session history")
        require(observer["status"] == "passed", "observer evidence did not combine lifecycle and session logs")
        require(
            ack_statuses == ["accepted", "executed", "local_effect_observed"],
            "archived acknowledgement stages were lost",
        )
        return {
            "session_archive_used": sources["session_archive_used"],
            "snapshot_count": len(sources["snapshots"]),
            "ack_statuses": ack_statuses,
            "observer_status": observer["status"],
        }


def check_lineage_session_archive_contract() -> Dict[str, Any]:
    original_session = "session-lineage-original"
    replay_session = "session-lineage-replay"
    command_id = "cmd-runtime-pause-lineage-fixture"

    def write_jsonl(path: pathlib.Path, records: List[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="oni-runtime-lineage-") as temporary:
        root = pathlib.Path(temporary)
        original_records = [
            {
                "event": "command_ack",
                "session_id": original_session,
                "id": command_id,
                "status": status,
            }
            for status in ("accepted", "executed", "local_effect_observed")
        ]
        replay_records = [
            {
                "event": "command_ack",
                "session_id": replay_session,
                "id": command_id,
                "status": "skipped_stale",
            }
        ]
        write_jsonl(root / "sessions" / original_session / "state.jsonl", original_records)
        write_jsonl(root / "sessions" / replay_session / "state.jsonl", replay_records)

        sources = lineage_session_record_sources(
            root,
            {original_session, replay_session},
            replay_records,
        )
        commands = [
            {
                "id": command_id,
                "action": "set_paused",
                "session_id": original_session,
                "status": "confirmed",
                "_directory": "done",
                "_confirmation_contract": {
                    "accepted": True,
                    "mode": CONFIRMED_MUTATION_MODE,
                    "blockers": [],
                    "legacy_read_only": False,
                },
            },
            {"id": command_id, "session_id": original_session, "_directory": "quarantine"},
        ]
        duplicate = duplicate_check(commands, sources["records"], replay_session)
        wrong_session = duplicate_check(commands, sources["records"], "session-unrelated")
        same_session_records = sources["records"] + [
            {
                "event": "command_ack",
                "session_id": original_session,
                "id": command_id,
                "status": "skipped_stale",
            }
        ]
        same_session = duplicate_check(commands, same_session_records, original_session)
        statuses = [record.get("status") for record in sources["records"]]
        require(len(sources["archive_paths"]) == 2, "not all lineage session archives were selected")
        require(
            statuses == ["accepted", "executed", "local_effect_observed", "skipped_stale"],
            "cross-session acknowledgement history was incomplete",
        )
        require(duplicate["status"] == "passed", "duplicate replay evidence did not span lineage sessions")
        require(wrong_session["status"] == "failed", "duplicate replay accepted an unrelated checkpoint session")
        require(same_session["status"] == "failed", "duplicate replay accepted the original execution session")
        return {
            "archive_count": len(sources["archive_paths"]),
            "ack_statuses": statuses,
            "duplicate_status": duplicate["status"],
            "wrong_session_rejected": wrong_session["status"] == "failed",
            "same_session_rejected": same_session["status"] == "failed",
        }


def check_colony_plan_frozen_artifact_contract() -> Dict[str, Any]:
    session_id = "session-plan-artifact-fixture"
    save_hash = "sha256:" + "a" * 64
    report = {
        "schema": "oni.colony-plan-verification.v1",
        "mode": "live",
        "status": "passed",
        "session_id": session_id,
        "save_id_hash": save_hash,
        "plan_id": "plan-fixture",
        "final_status": "aborted",
        "command_requires_approval": True,
        "command_final_directory": "quarantine",
        "game_command_executed": False,
    }
    with tempfile.TemporaryDirectory(prefix="oni-plan-artifact-") as temporary:
        root = pathlib.Path(temporary)
        source = root / "colony-plan-live.json"
        payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        source.write_text(payload, encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        frozen = root / f"{digest.removeprefix('sha256:')}-colony-plan-live.json"
        frozen.write_bytes(source.read_bytes())
        manifest = {
            "checkpoints": [
                {
                    "step_id": "colony_plan_runtime",
                    "outcome": "control_plane_verified",
                    "command": None,
                    "before": {"session_id": session_id, "save_id_hash": save_hash},
                    "artifacts": [{"path": str(frozen), "sha256": digest}],
                }
            ]
        }
        previous = runtime_verifier.COLONY_PLAN_LIVE_REPORT
        try:
            runtime_verifier.COLONY_PLAN_LIVE_REPORT = source
            check = runtime_verifier.colony_plan_runtime_check({session_id}, manifest)
        finally:
            runtime_verifier.COLONY_PLAN_LIVE_REPORT = previous
        require(check["status"] == "passed", "frozen colony-plan report was not bound by content hash")
        frozen.write_text("tampered\n", encoding="utf-8")
        try:
            runtime_verifier.COLONY_PLAN_LIVE_REPORT = source
            tampered = runtime_verifier.colony_plan_runtime_check({session_id}, manifest)
        finally:
            runtime_verifier.COLONY_PLAN_LIVE_REPORT = previous
        require(tampered["status"] == "failed", "tampered frozen colony-plan report was accepted")
        return {
            "artifact_hash_bound": True,
            "tamper_rejected": True,
        }


def check_batch_d_contract() -> Dict[str, Any]:
    catalog = load_catalog()
    expected = {
        "create_colony_plan",
        "advance_colony_plan",
        "pause_colony_plan",
        "resume_colony_plan",
        "abort_colony_plan",
        "recover_colony_plan",
    }
    actions = {
        str(item.get("id"))
        for item in catalog.get("actions") or []
        if isinstance(item, dict) and item.get("batch") == "D"
    }
    require(actions == expected, "batch-D action set changed")
    require(actions.isdisjoint(ALLOWED_ACTIONS), "bridge plan controls entered the game command queue")
    required = (
        PROJECT_ROOT / "agent" / "colony_plan.py",
        PROJECT_ROOT / "scripts" / "manage_colony_plan.py",
        PROJECT_ROOT / "scripts" / "verify_colony_plan.py",
        PROJECT_ROOT / "scenarios" / "game_verify_runtime_plan.json",
    )
    require(all(path.exists() for path in required), "batch-D implementation or verification files are missing")
    source = required[0].read_text(encoding="utf-8")
    for marker in (
        'PLAN_STATUSES = {"draft", "active", "paused", "completed", "aborted", "failed"}',
        '"requires_approval": True',
        '"open_command_id"',
        '"previous_revision_hash"',
        '"colony_plan_aborted"',
        '"pending_human_approval"',
    ):
        require(marker in source, f"batch-D safety marker is missing: {marker}")
    return {
        "actions": sorted(actions),
        "game_queue_disjoint": True,
        "approval_gate": True,
        "immutable_revisions": True,
    }


def main() -> int:
    checks = []
    for name, callback in (
        ("catalog", check_catalog),
        ("batch_a_contract", check_batch_a_contract),
        ("batch_a_runtime_contract", check_batch_a_runtime_contract),
        ("batch_b_contract", check_batch_b_contract),
        ("batch_b_runtime_contract", check_batch_b_runtime_contract),
        ("batch_c_contract", check_batch_c_contract),
        ("batch_c_runtime_contract", check_batch_c_runtime_contract),
        ("runtime_rotated_log_contract", check_runtime_rotated_log_contract),
        ("lineage_session_archive_contract", check_lineage_session_archive_contract),
        ("colony_plan_frozen_artifact_contract", check_colony_plan_frozen_artifact_contract),
        ("batch_d_contract", check_batch_d_contract),
    ):
        try:
            details = callback()
            checks.append({"name": name, "status": "passed", "details": details})
            print(f"[PASSED] {name}: {json.dumps(details, ensure_ascii=False, sort_keys=True)}")
        except Exception as exc:
            checks.append({"name": name, "status": "failed", "classification": "behavior_fail", "error": str(exc)})
            print(f"[FAILED] {name}: {exc}")
    passed = all(item["status"] == "passed" for item in checks)
    report = {
        "schema": "oni.advanced-action-verification.v1",
        "status": "passed" if passed else "failed",
        "classification": None if passed else "behavior_fail",
        "checks": checks,
    }
    report_path = PROJECT_ROOT / "reports" / "advanced-actions.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
