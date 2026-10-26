from __future__ import annotations

import copy
import json
import pathlib
import re
from typing import Any, Dict, Iterable, List

from agent.field_contracts import build_condition, contract_id_for_path, read_contract_field


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "config" / "advanced-actions.v1.json"
MAX_DIG_CELLS = 32
MAX_BUILDING_BLUEPRINT_CELLS = 8
MAX_UTILITY_BLUEPRINT_CELLS = 64
MAX_PATH_ID_LENGTH = 64
# Single-building blueprints the executor accepts (mirrors the Mod's
# BuildingBlueprintAllowlist).  MineralDeoxidizer (Oxygen Diffuser) and
# AlgaeHabitat (Algae Terrarium) are the early oxygen buildings; both are 1x2
# and the Probe reports a candidate cell only when the whole footprint is empty,
# the building is researched and the game's own placement rules accept it.
BUILDING_BLUEPRINT_ALLOWLIST = {"Ladder", "MineralDeoxidizer", "AlgaeHabitat"}
UTILITY_BLUEPRINT_ALLOWLIST = {"Wire"}
BLUEPRINT_PRIORITY_CLASSES = {"idle", "basic", "high", "personalNeeds", "topPriority", "compulsory"}


def load_catalog(path: pathlib.Path = CATALOG_PATH) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema") != "oni.advanced-action-catalog.v1":
        raise ValueError("Advanced action catalog is missing or invalid.")
    return value


def duplicant_state_key(instance_id: int) -> str:
    if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0:
        raise ValueError("duplicant_instance_id must be a positive integer.")
    return f"id_{instance_id}"


def duplicant_priority_path(instance_id: int, chore_group: str) -> str:
    if not isinstance(chore_group, str) or not chore_group or not chore_group.isalnum():
        raise ValueError("chore_group must be a non-empty alphanumeric id.")
    return f"duplicants.by_instance_id.{duplicant_state_key(instance_id)}.chore_priorities.{chore_group}"


def dig_cell_key(cell: int) -> str:
    if isinstance(cell, bool) or not isinstance(cell, int) or cell < 0:
        raise ValueError("cell must be a non-negative integer.")
    return f"cell_{cell}"


def dig_cell_path(cell: int) -> str:
    return f"designations.dig.by_cell.{dig_cell_key(cell)}"


def blueprint_cell_path(cell: int) -> str:
    return f"cells.by_id.{dig_cell_key(cell)}"


def blueprint_instance_path(instance_id: int) -> str:
    if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0:
        raise ValueError("blueprint_instance_id must be a positive integer.")
    return f"blueprints.by_instance_id.id_{instance_id}"


def utility_path_key(path_id: str) -> str:
    if (
        not isinstance(path_id, str)
        or not path_id
        or len(path_id) > MAX_PATH_ID_LENGTH
        or not path_id.isalnum()
    ):
        raise ValueError("path_id must be a bounded non-empty alphanumeric id.")
    return f"id_{path_id}"


def utility_path_path(path_id: str) -> str:
    return f"blueprints.utility_paths.{utility_path_key(path_id)}"


def _required_contract_id(path: str) -> str:
    contract_id = contract_id_for_path(path)
    if contract_id is None:
        raise ValueError(f"{path} has no registered quality contract.")
    return contract_id


def _require_contract_value(state: Dict[str, Any], path: str) -> Any:
    contract_id = _required_contract_id(path)
    read = read_contract_field(
        state,
        path,
        contract_id,
        allowed_statuses=("observed",),
    )
    if read.blockers:
        raise ValueError(
            f"{path} is unavailable under {contract_id}: {', '.join(read.blockers)}."
        )
    return read.value


def _condition(path: str, op: str, value: Any) -> Dict[str, Any]:
    return build_condition(
        path,
        op,
        value,
        _required_contract_id(path),
        quality=("observed",),
    )


def _observed_eq(path: str, value: Any) -> Dict[str, Any]:
    return _condition(path, "eq", value)


def _require_paused(state: Dict[str, Any]) -> None:
    if _require_contract_value(state, "game.paused") is not True:
        raise ValueError("Advanced actions require observed game.paused=true.")


def _normalize_cells(
    cells: Iterable[int],
    *,
    maximum: int = MAX_DIG_CELLS,
    preserve_order: bool = False,
) -> List[int]:
    if isinstance(cells, (str, bytes)):
        raise ValueError("cells must be an iterable of integers.")
    result = list(cells)
    if not result or len(result) > maximum:
        raise ValueError(f"cells must contain 1 through {maximum} entries.")
    if any(isinstance(cell, bool) or not isinstance(cell, int) or cell < 0 for cell in result):
        raise ValueError("every cell must be a non-negative integer.")
    if len(set(result)) != len(result):
        raise ValueError("cells must not contain duplicates.")
    return result if preserve_order else sorted(result)


def _dig_cell_values(state: Dict[str, Any], cell: int) -> Dict[str, Any]:
    prefix = dig_cell_path(cell)
    fields = (
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
    return {field: _require_contract_value(state, f"{prefix}.{field}") for field in fields}


def _unchanged_conditions(prefix: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        _observed_eq(f"{prefix}.{field}", values[field])
        for field in ("element_id", "mass_kg", "damage")
    ]


def build_mark_dig_hint(state: Dict[str, Any], *, cells: Iterable[int]) -> Dict[str, Any]:
    _require_paused(state)
    if _require_contract_value(state, "designations.dig.tool.tiles_filter_on") is not True:
        raise ValueError("DigTool Tiles filter must be observed on.")
    if _require_contract_value(state, "designations.dig.tool.natural_backwall_filter_on") is not False:
        raise ValueError("DigTool NaturalBackwall filter must be observed off.")

    normalized = _normalize_cells(cells)
    preconditions: List[Dict[str, Any]] = [
        _observed_eq("game.paused", True),
        _observed_eq("designations.dig.tool.tiles_filter_on", True),
        _observed_eq("designations.dig.tool.natural_backwall_filter_on", False),
    ]
    effects: List[Dict[str, Any]] = []
    for cell in normalized:
        prefix = dig_cell_path(cell)
        values = _dig_cell_values(state, cell)
        if values["safe_mark_candidate"] is not True or values["designation_present"] is not False:
            raise ValueError(f"cell {cell} is not a safe unmarked dig candidate.")
        if not isinstance(values["fingerprint"], str) or not values["fingerprint"].startswith("sha256:"):
            raise ValueError(f"cell {cell} has an invalid state fingerprint.")

        preconditions.extend(
            [
                _observed_eq(f"{prefix}.fingerprint", values["fingerprint"]),
                _observed_eq(f"{prefix}.safe_mark_candidate", True),
                _observed_eq(f"{prefix}.designation_present", False),
                _observed_eq(f"{prefix}.solid", True),
            ]
        )
        preconditions.extend(_unchanged_conditions(prefix, values))
        effects.extend(
            [
                _observed_eq(f"{prefix}.designation_present", True),
                _condition(f"{prefix}.designation_instance_id", "gt", 0),
                _observed_eq(f"{prefix}.designation_kind", "tile"),
                _observed_eq(f"{prefix}.solid", True),
                _observed_eq(f"{prefix}.safe_mark_candidate", False),
            ]
        )
        effects.extend(_unchanged_conditions(prefix, values))

    return {
        "action": "mark_dig_cells",
        "args": {"cells": normalized},
        "preconditions": preconditions,
        "expected_effects": effects,
    }


def build_cancel_dig_hint(state: Dict[str, Any], *, cells: Iterable[int]) -> Dict[str, Any]:
    _require_paused(state)
    normalized = _normalize_cells(cells)
    targets: List[Dict[str, int]] = []
    preconditions: List[Dict[str, Any]] = [_observed_eq("game.paused", True)]
    effects: List[Dict[str, Any]] = []
    for cell in normalized:
        prefix = dig_cell_path(cell)
        values = _dig_cell_values(state, cell)
        instance_id = values["designation_instance_id"]
        if (
            values["designation_present"] is not True
            or isinstance(instance_id, bool)
            or not isinstance(instance_id, int)
            or instance_id <= 0
            or values["designation_kind"] != "tile"
            or values["designation_in_progress"] is not False
        ):
            raise ValueError(f"cell {cell} is not a cancellable tile-only dig designation.")
        if not isinstance(values["fingerprint"], str) or not values["fingerprint"].startswith("sha256:"):
            raise ValueError(f"cell {cell} has an invalid state fingerprint.")

        targets.append({"cell": cell, "designation_instance_id": instance_id})
        preconditions.extend(
            [
                _observed_eq(f"{prefix}.fingerprint", values["fingerprint"]),
                _observed_eq(f"{prefix}.designation_instance_id", instance_id),
                _observed_eq(f"{prefix}.designation_kind", "tile"),
                _observed_eq(f"{prefix}.designation_in_progress", False),
                _observed_eq(f"{prefix}.solid", True),
            ]
        )
        preconditions.extend(_unchanged_conditions(prefix, values))
        effects.extend(
            [
                _observed_eq(f"{prefix}.designation_present", False),
                _observed_eq(f"{prefix}.designation_instance_id", 0),
                _observed_eq(f"{prefix}.designation_kind", "none"),
                _observed_eq(f"{prefix}.solid", True),
                _observed_eq(f"{prefix}.safe_mark_candidate", True),
            ]
        )
        effects.extend(_unchanged_conditions(prefix, values))

    return {
        "action": "cancel_dig_cells",
        "args": {"targets": targets},
        "preconditions": preconditions,
        "expected_effects": effects,
    }


BLUEPRINT_CELL_FIELDS = (
    "cell",
    "x",
    "y",
    "world_id",
    "element_id",
    "mass_kg",
    "damage",
    "object_count",
    "empty_for_blueprint",
    "can_place_ladder",
    "can_place_wire",
    "blueprint_present",
    "blueprint_instance_id",
    "blueprint_prefab_id",
    "blueprint_orientation",
    "blueprint_material_tags",
    "blueprint_in_progress",
    "blueprint_is_utility",
    "empty_fingerprint",
    "fingerprint",
)


def blueprint_can_place_field(building_def_id: str) -> str:
    """The Probe's per-cell placement field for one building (Ladder -> can_place_ladder)."""

    return "can_place_" + re.sub(r"(?<!^)(?=[A-Z])", "_", building_def_id).lower()


def _require_blueprint_environment(state: Dict[str, Any]) -> None:
    _require_paused(state)
    if _require_contract_value(state, "game.sandbox_mode") is not False:
        raise ValueError("Blueprint actions require observed game.sandbox_mode=false.")
    if _require_contract_value(state, "game.instant_build_mode") is not False:
        raise ValueError("Blueprint actions require observed game.instant_build_mode=false.")


def _blueprint_environment_conditions() -> List[Dict[str, Any]]:
    return [
        _observed_eq("game.paused", True),
        _observed_eq("game.sandbox_mode", False),
        _observed_eq("game.instant_build_mode", False),
    ]


def _normalize_material_tags(material_tags: Iterable[str]) -> List[str]:
    if isinstance(material_tags, (str, bytes)):
        raise ValueError("material_tags must be an iterable of tag ids.")
    result = list(material_tags)
    if not result or len(result) > 8:
        raise ValueError("material_tags must contain 1 through 8 entries.")
    for tag in result:
        if (
            not isinstance(tag, str)
            or not tag
            or len(tag) > 64
            or any(not (character.isalnum() or character == "_") for character in tag)
        ):
            raise ValueError("material_tags must contain bounded alphanumeric or underscore ids.")
    return result


def _blueprint_catalog_values(
    state: Dict[str, Any],
    building_def_id: str,
    material_tags: Iterable[str],
    *,
    is_utility: bool,
) -> tuple[List[str], Dict[str, Any]]:
    normalized_materials = _normalize_material_tags(material_tags)
    prefix = f"blueprints.catalog.{building_def_id}"
    fields = (
        "available",
        "prefab_id",
        "is_utility",
        "cancellable",
        "orientation",
        "facade_id",
        "material_tags",
    )
    values = {field: _require_contract_value(state, f"{prefix}.{field}") for field in fields}
    if (
        values["available"] is not True
        or values["prefab_id"] != building_def_id
        or values["is_utility"] is not is_utility
        or values["cancellable"] is not True
        or values["orientation"] != "Neutral"
        or values["facade_id"] != "DEFAULT_FACADE"
        or values["material_tags"] != normalized_materials
    ):
        raise ValueError(f"{building_def_id} catalog state does not satisfy the blueprint contract.")
    return normalized_materials, values


def _blueprint_catalog_conditions(
    building_def_id: str,
    material_tags: List[str],
    *,
    is_utility: bool,
) -> List[Dict[str, Any]]:
    prefix = f"blueprints.catalog.{building_def_id}"
    return [
        _observed_eq(f"{prefix}.available", True),
        _observed_eq(f"{prefix}.prefab_id", building_def_id),
        _observed_eq(f"{prefix}.is_utility", is_utility),
        _observed_eq(f"{prefix}.cancellable", True),
        _observed_eq(f"{prefix}.orientation", "Neutral"),
        _observed_eq(f"{prefix}.facade_id", "DEFAULT_FACADE"),
        _observed_eq(f"{prefix}.material_tags", material_tags),
    ]


def _blueprint_cell_values(
    state: Dict[str, Any], cell: int, extra_fields: Iterable[str] = ()
) -> Dict[str, Any]:
    # The per-building placement field is read only for the building being
    # placed or cancelled, so recordings and fixtures from a Probe that knew
    # fewer buildings still validate for the buildings they report.
    prefix = blueprint_cell_path(cell)
    fields = BLUEPRINT_CELL_FIELDS + tuple(field for field in extra_fields if field not in BLUEPRINT_CELL_FIELDS)
    return {field: _require_contract_value(state, f"{prefix}.{field}") for field in fields}


def _blueprint_physical_conditions(prefix: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [_observed_eq(f"{prefix}.{field}", values[field]) for field in ("element_id", "mass_kg", "damage")]


def _build_blueprint_place_contract(
    state: Dict[str, Any],
    cells: Iterable[int],
    building_def_id: str,
    material_tags: Iterable[str],
    *,
    is_utility: bool,
    maximum: int,
    preserve_order: bool,
) -> tuple[List[int], List[str], List[Dict[str, Any]], List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    _require_blueprint_environment(state)
    normalized_cells = _normalize_cells(cells, maximum=maximum, preserve_order=preserve_order)
    normalized_materials, _ = _blueprint_catalog_values(
        state,
        building_def_id,
        material_tags,
        is_utility=is_utility,
    )
    can_place_field = "can_place_wire" if is_utility else blueprint_can_place_field(building_def_id)
    preconditions = _blueprint_environment_conditions()
    preconditions.extend(
        _blueprint_catalog_conditions(
            building_def_id,
            normalized_materials,
            is_utility=is_utility,
        )
    )
    effects: List[Dict[str, Any]] = []
    values_by_cell: Dict[int, Dict[str, Any]] = {}
    for cell in normalized_cells:
        prefix = blueprint_cell_path(cell)
        values = _blueprint_cell_values(state, cell, (can_place_field,))
        values_by_cell[cell] = values
        if (
            values["cell"] != cell
            or values["object_count"] != 0
            or values["empty_for_blueprint"] is not True
            or values[can_place_field] is not True
            or values["blueprint_present"] is not False
            or values["blueprint_instance_id"] != 0
            or values["blueprint_prefab_id"] != "none"
            or values["fingerprint"] != values["empty_fingerprint"]
            or not isinstance(values["fingerprint"], str)
            or not values["fingerprint"].startswith("sha256:")
        ):
            raise ValueError(f"cell {cell} is not a verified empty {building_def_id} target.")
        preconditions.extend(
            [
                _observed_eq(f"{prefix}.fingerprint", values["fingerprint"]),
                _observed_eq(f"{prefix}.empty_fingerprint", values["empty_fingerprint"]),
                _observed_eq(f"{prefix}.object_count", 0),
                _observed_eq(f"{prefix}.empty_for_blueprint", True),
                _observed_eq(f"{prefix}.{can_place_field}", True),
                _observed_eq(f"{prefix}.blueprint_present", False),
                _observed_eq(f"{prefix}.blueprint_instance_id", 0),
            ]
        )
        preconditions.extend(_blueprint_physical_conditions(prefix, values))
        effects.extend(
            [
                _observed_eq(f"{prefix}.empty_fingerprint", values["empty_fingerprint"]),
                _condition(f"{prefix}.fingerprint", "ne", values["fingerprint"]),
                _condition(f"{prefix}.object_count", "gt", 0),
                _observed_eq(f"{prefix}.empty_for_blueprint", False),
                _observed_eq(f"{prefix}.{can_place_field}", False),
                _observed_eq(f"{prefix}.blueprint_present", True),
                _condition(f"{prefix}.blueprint_instance_id", "gt", 0),
                _observed_eq(f"{prefix}.blueprint_prefab_id", building_def_id),
                _observed_eq(f"{prefix}.blueprint_orientation", "Neutral"),
                _observed_eq(f"{prefix}.blueprint_material_tags", normalized_materials),
                _observed_eq(f"{prefix}.blueprint_in_progress", False),
                _observed_eq(f"{prefix}.blueprint_is_utility", is_utility),
            ]
        )
        effects.extend(_blueprint_physical_conditions(prefix, values))
    return normalized_cells, normalized_materials, preconditions, effects, values_by_cell


def _build_blueprint_cancel_target_contract(
    state: Dict[str, Any],
    *,
    cell: int,
    instance_id: int,
    building_def_id: str,
    is_utility: bool,
) -> tuple[Dict[str, int], List[Dict[str, Any]], List[Dict[str, Any]]]:
    prefix = blueprint_cell_path(cell)
    can_place_field = "can_place_wire" if is_utility else blueprint_can_place_field(building_def_id)
    values = _blueprint_cell_values(state, cell, (can_place_field,))
    instance_prefix = blueprint_instance_path(instance_id)
    instance_values = {
        field: _require_contract_value(state, f"{instance_prefix}.{field}")
        for field in ("instance_id", "cell", "prefab_id", "orientation", "material_tags", "in_progress", "is_utility")
    }
    if (
        values["cell"] != cell
        or values["blueprint_present"] is not True
        or values["blueprint_instance_id"] != instance_id
        or values["blueprint_prefab_id"] != building_def_id
        or values["blueprint_orientation"] != "Neutral"
        or not isinstance(values["blueprint_material_tags"], list)
        or not values["blueprint_material_tags"]
        or values["blueprint_in_progress"] is not False
        or values["blueprint_is_utility"] is not is_utility
        or not isinstance(values["object_count"], int)
        or values["object_count"] <= 0
        or not isinstance(values["empty_fingerprint"], str)
        or not values["empty_fingerprint"].startswith("sha256:")
        or instance_values["instance_id"] != instance_id
        or instance_values["cell"] != cell
        or instance_values["prefab_id"] != building_def_id
        or instance_values["orientation"] != "Neutral"
        or instance_values["material_tags"] != values["blueprint_material_tags"]
        or instance_values["in_progress"] is not False
        or instance_values["is_utility"] is not is_utility
    ):
        raise ValueError(f"cell {cell} does not contain the exact cancellable blueprint instance.")
    preconditions = [
        _observed_eq(f"{prefix}.fingerprint", values["fingerprint"]),
        _observed_eq(f"{prefix}.empty_fingerprint", values["empty_fingerprint"]),
        _observed_eq(f"{prefix}.blueprint_present", True),
        _observed_eq(f"{prefix}.blueprint_instance_id", instance_id),
        _observed_eq(f"{prefix}.blueprint_prefab_id", building_def_id),
        _observed_eq(f"{prefix}.blueprint_orientation", "Neutral"),
        _observed_eq(f"{prefix}.blueprint_material_tags", values["blueprint_material_tags"]),
        _observed_eq(f"{prefix}.blueprint_in_progress", False),
        _observed_eq(f"{prefix}.blueprint_is_utility", is_utility),
        _observed_eq(f"{instance_prefix}.instance_id", instance_id),
        _observed_eq(f"{instance_prefix}.cell", cell),
        _observed_eq(f"{instance_prefix}.prefab_id", building_def_id),
        _observed_eq(f"{instance_prefix}.in_progress", False),
    ]
    preconditions.extend(_blueprint_physical_conditions(prefix, values))
    effects = [
        _observed_eq(f"{prefix}.fingerprint", values["empty_fingerprint"]),
        _observed_eq(f"{prefix}.empty_fingerprint", values["empty_fingerprint"]),
        _observed_eq(f"{prefix}.object_count", 0),
        _observed_eq(f"{prefix}.empty_for_blueprint", True),
        _observed_eq(f"{prefix}.{can_place_field}", True),
        _observed_eq(f"{prefix}.blueprint_present", False),
        _observed_eq(f"{prefix}.blueprint_instance_id", 0),
        _observed_eq(f"{prefix}.blueprint_prefab_id", "none"),
        _observed_eq(f"{prefix}.blueprint_orientation", "none"),
        _observed_eq(f"{prefix}.blueprint_material_tags", []),
        _observed_eq(f"{prefix}.blueprint_in_progress", False),
        _observed_eq(f"{prefix}.blueprint_is_utility", False),
    ]
    effects.extend(_blueprint_physical_conditions(prefix, values))
    return {"cell": cell, "blueprint_instance_id": instance_id}, preconditions, effects


def build_place_building_blueprint_hint(
    state: Dict[str, Any],
    *,
    cells: Iterable[int],
    material_tags: Iterable[str],
    building_def_id: str = "Ladder",
) -> Dict[str, Any]:
    if building_def_id not in BUILDING_BLUEPRINT_ALLOWLIST:
        raise ValueError("building_def_id is not in the single-building blueprint allowlist.")
    normalized_cells, normalized_materials, preconditions, effects, _ = _build_blueprint_place_contract(
        state,
        cells,
        building_def_id,
        material_tags,
        is_utility=False,
        maximum=MAX_BUILDING_BLUEPRINT_CELLS,
        preserve_order=False,
    )
    return {
        "action": "place_building_blueprint",
        "args": {
            "building_def_id": building_def_id,
            "cells": normalized_cells,
            "orientation": "Neutral",
            "material_tags": normalized_materials,
            "facade_id": "DEFAULT_FACADE",
        },
        "preconditions": preconditions,
        "expected_effects": effects,
    }


def build_cancel_building_blueprint_hint(
    state: Dict[str, Any],
    *,
    cells: Iterable[int],
) -> Dict[str, Any]:
    _require_blueprint_environment(state)
    normalized_cells = _normalize_cells(cells, maximum=MAX_BUILDING_BLUEPRINT_CELLS)
    targets: List[Dict[str, int]] = []
    preconditions = _blueprint_environment_conditions()
    effects: List[Dict[str, Any]] = []
    common_prefab_id: str | None = None
    for cell in normalized_cells:
        values = _blueprint_cell_values(state, cell)
        instance_id = values["blueprint_instance_id"]
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0:
            raise ValueError(f"cell {cell} does not expose a positive blueprint instance id.")
        prefab_id = values["blueprint_prefab_id"]
        if prefab_id not in BUILDING_BLUEPRINT_ALLOWLIST:
            raise ValueError(f"cell {cell} holds a blueprint outside the single-building allowlist.")
        if common_prefab_id is None:
            common_prefab_id = prefab_id
        elif prefab_id != common_prefab_id:
            raise ValueError(
                "cancel_building_blueprint must bind one allowlisted observed "
                "blueprint_prefab_id for every target."
            )
        target, target_preconditions, target_effects = _build_blueprint_cancel_target_contract(
            state,
            cell=cell,
            instance_id=instance_id,
            building_def_id=prefab_id,
            is_utility=False,
        )
        targets.append(target)
        preconditions.extend(target_preconditions)
        effects.extend(target_effects)
    return {
        "action": "cancel_building_blueprint",
        "args": {"targets": targets},
        "preconditions": preconditions,
        "expected_effects": effects,
    }


def build_place_utility_blueprint_path_hint(
    state: Dict[str, Any],
    *,
    path_id: str,
    cells: Iterable[int],
    material_tags: Iterable[str],
    building_def_id: str = "Wire",
) -> Dict[str, Any]:
    if building_def_id not in UTILITY_BLUEPRINT_ALLOWLIST:
        raise ValueError("building_def_id is not in the utility blueprint allowlist.")
    path_key = utility_path_key(path_id)
    normalized_cells, normalized_materials, preconditions, effects, values_by_cell = (
        _build_blueprint_place_contract(
            state,
            cells,
            building_def_id,
            material_tags,
            is_utility=True,
            maximum=MAX_UTILITY_BLUEPRINT_CELLS,
            preserve_order=True,
        )
    )
    for left, right in zip(normalized_cells, normalized_cells[1:]):
        left_values = values_by_cell[left]
        right_values = values_by_cell[right]
        if abs(left_values["x"] - right_values["x"]) + abs(left_values["y"] - right_values["y"]) != 1:
            raise ValueError("utility blueprint cells must form one ordered four-neighbor path.")
    path_prefix = f"blueprints.utility_paths.{path_key}"
    effects.extend(
        [
            _observed_eq(f"{path_prefix}.path_id", path_id),
            _observed_eq(f"{path_prefix}.building_def_id", building_def_id),
            _observed_eq(f"{path_prefix}.cells", normalized_cells),
            _observed_eq(f"{path_prefix}.active", True),
            _observed_eq(f"{path_prefix}.fully_active", True),
        ]
    )
    return {
        "action": "place_utility_blueprint_path",
        "args": {
            "path_id": path_id,
            "building_def_id": building_def_id,
            "cells": normalized_cells,
            "orientation": "Neutral",
            "material_tags": normalized_materials,
            "facade_id": "DEFAULT_FACADE",
        },
        "preconditions": preconditions,
        "expected_effects": effects,
    }


def build_cancel_utility_blueprint_path_hint(
    state: Dict[str, Any],
    *,
    path_id: str,
) -> Dict[str, Any]:
    _require_blueprint_environment(state)
    prefix = utility_path_path(path_id)
    path_values = {
        field: _require_contract_value(state, f"{prefix}.{field}")
        for field in (
            "path_id",
            "building_def_id",
            "cells",
            "blueprint_instance_ids",
            "active_blueprint_instance_ids",
            "active",
            "fully_active",
        )
    }
    cells = path_values["cells"]
    instance_ids = path_values["blueprint_instance_ids"]
    if (
        path_values["path_id"] != path_id
        or path_values["building_def_id"] != "Wire"
        or path_values["active"] is not True
        or path_values["fully_active"] is not True
        or not isinstance(cells, list)
        or not isinstance(instance_ids, list)
        or len(cells) < 1
        or len(cells) > MAX_UTILITY_BLUEPRINT_CELLS
        or len(instance_ids) != len(cells)
        or path_values["active_blueprint_instance_ids"] != instance_ids
    ):
        raise ValueError("utility path is not an exact fully-active Wire blueprint path.")
    normalized_cells = _normalize_cells(
        cells,
        maximum=MAX_UTILITY_BLUEPRINT_CELLS,
        preserve_order=True,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in instance_ids):
        raise ValueError("utility path contains an invalid blueprint instance id.")
    targets: List[Dict[str, int]] = []
    preconditions = _blueprint_environment_conditions()
    preconditions.extend(
        [
            _observed_eq(f"{prefix}.path_id", path_id),
            _observed_eq(f"{prefix}.building_def_id", "Wire"),
            _observed_eq(f"{prefix}.cells", normalized_cells),
            _observed_eq(f"{prefix}.blueprint_instance_ids", instance_ids),
            _observed_eq(f"{prefix}.active_blueprint_instance_ids", instance_ids),
            _observed_eq(f"{prefix}.active", True),
            _observed_eq(f"{prefix}.fully_active", True),
        ]
    )
    effects: List[Dict[str, Any]] = []
    for cell, instance_id in zip(normalized_cells, instance_ids):
        target, target_preconditions, target_effects = _build_blueprint_cancel_target_contract(
            state,
            cell=cell,
            instance_id=instance_id,
            building_def_id="Wire",
            is_utility=True,
        )
        targets.append(target)
        preconditions.extend(target_preconditions)
        effects.extend(target_effects)
    effects.extend(
        [
            _observed_eq(f"{prefix}.path_id", path_id),
            _observed_eq(f"{prefix}.building_def_id", "Wire"),
            _observed_eq(f"{prefix}.cells", normalized_cells),
            _observed_eq(f"{prefix}.blueprint_instance_ids", instance_ids),
            _observed_eq(f"{prefix}.active_blueprint_instance_ids", []),
            _observed_eq(f"{prefix}.active", False),
            _observed_eq(f"{prefix}.fully_active", False),
        ]
    )
    return {
        "action": "cancel_utility_blueprint_path",
        "args": {"path_id": path_id, "targets": targets},
        "preconditions": preconditions,
        "expected_effects": effects,
    }


MAX_RESEARCH_ID_LENGTH = 128  # ActionExecutor.MaxResearchIdLength


def build_set_research_hint(state: Dict[str, Any], *, tech: str) -> Dict[str, Any]:
    """Build the executor's ``set_research`` contract for one tech id.

    The executor requires the precondition to bind the observed
    ``research.current`` value (which must differ from ``tech``) and the
    expected effect ``research.current == tech``.  It validates the tech id
    against ``Db.Techs`` itself and refuses a finished tech or one with
    unfinished prerequisites deterministically, so this builder only needs the
    observed current selection.  Research needs no pause.
    """

    if (
        not isinstance(tech, str)
        or not tech
        or len(tech) > MAX_RESEARCH_ID_LENGTH
        or not tech.isalnum()
        or not tech.isascii()
    ):
        raise ValueError("tech must be a bounded ASCII alphanumeric technology id.")
    current = _require_contract_value(state, "research.current")
    if not isinstance(current, str):
        raise ValueError("research.current must be an observed string (empty when idle).")
    if current == tech:
        raise ValueError(f"research.current already equals {tech}.")
    return {
        "action": "set_research",
        "args": {"tech": tech},
        "preconditions": [_observed_eq("research.current", current)],
        "expected_effects": [_observed_eq("research.current", tech)],
    }


def build_duplicant_priority_hint(
    state: Dict[str, Any],
    *,
    instance_id: int,
    chore_group: str,
    priority: int,
) -> Dict[str, Any]:
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 5:
        raise ValueError("priority must be an integer from 0 through 5.")

    _require_paused(state)
    key = duplicant_state_key(instance_id)
    duplicants = state.get("duplicants") if isinstance(state.get("duplicants"), dict) else {}
    by_instance = duplicants.get("by_instance_id") if isinstance(duplicants.get("by_instance_id"), dict) else {}
    target = by_instance.get(key) if isinstance(by_instance.get(key), dict) else None
    if target is None or target.get("instance_id") != instance_id:
        raise ValueError("Target duplicant instance is unavailable in the current state.")

    expected_name = target.get("name")
    if not isinstance(expected_name, str) or not expected_name.strip():
        raise ValueError("Target duplicant name is unavailable.")

    path = duplicant_priority_path(instance_id, chore_group)
    current = _require_contract_value(state, path)
    if isinstance(current, bool) or not isinstance(current, int) or not 0 <= current <= 5:
        raise ValueError("Target chore priority is unavailable or invalid.")
    if current == priority:
        raise ValueError("Priority command must produce an actual state change.")

    return {
        "action": "set_duplicant_chore_priority",
        "args": {
            "duplicant_instance_id": instance_id,
            "expected_name": expected_name,
            "chore_group": chore_group,
            "from_priority": current,
            "priority": priority,
        },
        "preconditions": [
            _observed_eq("game.paused", True),
            _observed_eq(path, current),
        ],
        "expected_effects": [_observed_eq(path, priority)],
    }


def build_constructable_priority_hint(
    state: Dict[str, Any],
    *,
    instance_id: int,
    priority_class: str,
    priority_value: int,
) -> Dict[str, Any]:
    """Build a typed hint for an observed-zero-progress Constructable.

    This emits only a typed hint. The action is intentionally not in the
    current executor allowlist until its native writer and later-witness path
    are implemented. Missing progress remains blocked: the current telemetry
    omits both the native -1 sentinel and invalid raw progress, so omission
    cannot itself prove the Constructable is unstarted.
    """
    _require_blueprint_environment(state)
    if not isinstance(priority_class, str) or priority_class not in BLUEPRINT_PRIORITY_CLASSES:
        raise ValueError("priority_class is not a native PriorityScreen class.")
    if isinstance(priority_value, bool) or not isinstance(priority_value, int) or not 1 <= priority_value <= 9:
        raise ValueError("priority_value must be an integer from 1 through 9.")
    prefix = blueprint_instance_path(instance_id)
    fields = (
        "instance_id", "cell", "world_id", "prefab_id", "orientation", "material_tags",
        "in_progress", "is_utility", "fingerprint", "progress_percent", "worker_instance_id",
        "priority_class", "priority_value", "root_cell", "footprint_cells",
    )
    values = {field: _require_contract_value(state, f"{prefix}.{field}") for field in fields}
    world_id = state.get("world_id")
    if (
        isinstance(world_id, bool) or not isinstance(world_id, int)
        or world_id < 0 or values["instance_id"] != instance_id
        or values["world_id"] != world_id
    ):
        raise ValueError("Constructable identity is not bound to the active world.")
    if values["cell"] != values["root_cell"] or values["root_cell"] not in values["footprint_cells"]:
        raise ValueError("Constructable root is not bound to its footprint.")
    if not values["material_tags"]:
        raise ValueError("Constructable material identity is unavailable.")
    if values["in_progress"] is not False or values["progress_percent"] != 0 or values["worker_instance_id"] != 0:
        raise ValueError("Priority commitment requires an unstarted Constructable with no worker.")
    if (
        values["priority_class"] not in BLUEPRINT_PRIORITY_CLASSES
        or (
            values["priority_value"] == priority_value
            and values["priority_class"] == priority_class
        )
    ):
        raise ValueError("Current Constructable priority is unavailable or unchanged.")
    identity_fields = tuple(field for field in fields if field not in {"priority_class", "priority_value"})
    identity_conditions = [_observed_eq(f"{prefix}.{field}", values[field]) for field in identity_fields]
    preconditions = _blueprint_environment_conditions() + identity_conditions
    preconditions.extend(_observed_eq(f"{prefix}.{field}", values[field]) for field in ("priority_class", "priority_value"))
    expected_effects = list(identity_conditions)

    # Bind every footprint cell to the same native instance, not just a mutable
    # registry entry. Cell fingerprints also bind the physical from-state.
    cell_fields = (
        "cell", "world_id", "blueprint_present", "blueprint_instance_id",
        "blueprint_prefab_id", "blueprint_orientation", "blueprint_material_tags",
        "blueprint_in_progress", "blueprint_is_utility", "blueprint_root_cell",
        "blueprint_footprint_cells", "blueprint_progress_percent", "blueprint_worker_instance_id",
        "blueprint_priority_class", "blueprint_priority_value", "fingerprint",
        "element_id", "mass_kg", "damage",
    )
    cell_fingerprints = set()
    for cell in values["footprint_cells"]:
        cell_prefix = blueprint_cell_path(cell)
        cell_values = {field: _require_contract_value(state, f"{cell_prefix}.{field}") for field in cell_fields}
        expected_identity = {
            "cell": cell, "world_id": world_id, "blueprint_present": True,
            "blueprint_instance_id": instance_id, "blueprint_prefab_id": values["prefab_id"],
            "blueprint_orientation": values["orientation"], "blueprint_material_tags": values["material_tags"],
            "blueprint_in_progress": False, "blueprint_is_utility": values["is_utility"],
            "blueprint_root_cell": values["root_cell"], "blueprint_footprint_cells": values["footprint_cells"],
            "blueprint_progress_percent": 0, "blueprint_worker_instance_id": 0,
            "blueprint_priority_class": values["priority_class"], "blueprint_priority_value": values["priority_value"],
        }
        if any(cell_values[field] != expected for field, expected in expected_identity.items()):
            raise ValueError(f"Footprint cell {cell} does not match the exact Constructable from-state.")
        cell_fingerprints.add(cell_values["fingerprint"])
        preconditions.extend(_observed_eq(f"{cell_prefix}.{field}", cell_values[field]) for field in cell_fields)
        expected_effects.extend(
            _observed_eq(f"{cell_prefix}.{field}", cell_values[field])
            for field in cell_fields if field not in {"blueprint_priority_class", "blueprint_priority_value"}
        )
        expected_effects.extend([
            _observed_eq(f"{cell_prefix}.blueprint_priority_class", priority_class),
            _observed_eq(f"{cell_prefix}.blueprint_priority_value", priority_value),
        ])
    if values["fingerprint"] not in cell_fingerprints:
        raise ValueError("Constructable fingerprint is not bound to an observed footprint cell.")
    expected_effects.extend(
        [_observed_eq(f"{prefix}.priority_class", priority_class),
         _observed_eq(f"{prefix}.priority_value", priority_value)]
    )
    return copy.deepcopy({
        "action": "set_constructable_priority",
        "args": {
            "blueprint_instance_id": instance_id,
            "cell": values["cell"],
            "expected_prefab_id": values["prefab_id"],
            "expected_world_id": world_id,
            "expected_root_cell": values["root_cell"],
            "expected_footprint_cells": list(values["footprint_cells"]),
            "from_priority_class": values["priority_class"],
            "from_priority_value": values["priority_value"],
            "priority_class": priority_class,
            "priority_value": priority_value,
        },
        "preconditions": preconditions,
        "expected_effects": expected_effects,
    })
