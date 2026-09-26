#!/usr/bin/env python3
"""Offline contracts for a pre-authority, observed-zero-progress priority hint."""
from __future__ import annotations

import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.action_policy import ALLOWED_ACTIONS
from agent.advanced_actions import build_constructable_priority_hint
from agent.field_contracts import contract_for_path, contract_quality_fixture
from agent.state_quality import safe_get

INSTANCE = "blueprints.by_instance_id.id_42"


def fixture():
    instance = {
        "instance_id": 42, "cell": 100, "world_id": 0, "prefab_id": "Ladder",
        "orientation": "Neutral", "material_tags": ["SandStone"], "in_progress": False,
        "is_utility": False, "fingerprint": "sha256:" + "a" * 64,
        "progress_percent": 0, "worker_instance_id": 0, "root_cell": 100,
        "footprint_cells": [100, 101], "priority_class": "basic", "priority_value": 5,
    }
    state = {
        "schema": "oni.state.v2", "ts": "2026-09-13T03:45:00+00:00", "world_id": 0,
        "game": {"paused": True, "sandbox_mode": False, "instant_build_mode": False},
        "blueprints": {"by_instance_id": {"id_42": instance}},
        "cells": {"by_id": {}}, "quality": {},
    }
    for cell in (100, 101):
        state["cells"]["by_id"][f"cell_{cell}"] = {
            "cell": cell, "world_id": 0, "blueprint_present": True,
            "blueprint_instance_id": 42, "blueprint_prefab_id": "Ladder",
            "blueprint_orientation": "Neutral", "blueprint_material_tags": ["SandStone"],
            "blueprint_in_progress": False, "blueprint_is_utility": False,
            "blueprint_root_cell": 100, "blueprint_footprint_cells": [100, 101],
            "blueprint_progress_percent": 0, "blueprint_worker_instance_id": 0,
            "blueprint_priority_class": "basic", "blueprint_priority_value": 5,
            "fingerprint": "sha256:" + ("a" if cell == 100 else "b") * 64,
            "element_id": "Oxygen", "mass_kg": 1.0, "damage": 0.0,
        }
    paths = [f"game.{field}" for field in state["game"]]
    paths += [f"{INSTANCE}.{field}" for field in instance]
    paths += [f"cells.by_id.{key}.{field}" for key, values in state["cells"]["by_id"].items() for field in values]
    for path in paths:
        contract = contract_for_path(path)
        assert contract is not None, path
        state["quality"][path] = contract_quality_fixture(contract.contract_id)
    return state


def write_path(state, path, value):
    cursor = state
    keys = path.split(".")
    for key in keys[:-1]:
        cursor = cursor[key]
    cursor[keys[-1]] = value


def main():
    results = []
    state = fixture()
    original = copy.deepcopy(state)
    hint = build_constructable_priority_hint(state, instance_id=42, priority_class="high", priority_value=3)
    assert state == original
    assert hint["action"] == "set_constructable_priority" and hint["action"] not in ALLOWED_ACTIONS
    assert hint["args"]["expected_world_id"] == 0
    assert hint["args"]["expected_root_cell"] == 100
    assert hint["args"]["expected_footprint_cells"] == [100, 101]
    assert hint["args"]["from_priority_class"] == "basic" and hint["args"]["from_priority_value"] == 5
    assert all(c["op"] == "eq" and safe_get(state, c["path"]) == c["value"] for c in hint["preconditions"])
    after = copy.deepcopy(state)
    for prefix, class_field, value_field in [
        (INSTANCE, "priority_class", "priority_value"),
        ("cells.by_id.cell_100", "blueprint_priority_class", "blueprint_priority_value"),
        ("cells.by_id.cell_101", "blueprint_priority_class", "blueprint_priority_value"),
    ]:
        write_path(after, f"{prefix}.{class_field}", "high")
        write_path(after, f"{prefix}.{value_field}", 3)
    assert all(safe_get(after, c["path"]) == c["value"] for c in hint["expected_effects"])
    for condition in hint["preconditions"]:
        if condition["path"].endswith("material_tags"):
            condition["value"].append("Iron")
            break
    assert state == original, "hint values must not alias source state"
    results.append("exact_multicell_from_and_after_contract")

    changes = [
        ("progress_started", f"{INSTANCE}.progress_percent", 1),
        ("worker_present", f"{INSTANCE}.worker_instance_id", 9),
        ("in_progress", f"{INSTANCE}.in_progress", True),
        ("snapshot_world_mismatch", "world_id", 1),
        ("boolean_snapshot_world", "world_id", False),
        ("instance_mismatch", f"{INSTANCE}.instance_id", 43),
        ("root_mismatch", f"{INSTANCE}.root_cell", 101),
        ("empty_footprint", f"{INSTANCE}.footprint_cells", []),
        ("duplicate_footprint", f"{INSTANCE}.footprint_cells", [100, 100]),
        ("unsorted_footprint", f"{INSTANCE}.footprint_cells", [101, 100]),
        ("empty_materials", f"{INSTANCE}.material_tags", []),
        ("fingerprint_unbound", f"{INSTANCE}.fingerprint", "sha256:" + "c" * 64),
        ("cell_instance_mismatch", "cells.by_id.cell_101.blueprint_instance_id", 43),
        ("cell_world_mismatch", "cells.by_id.cell_101.world_id", 1),
        ("cell_priority_mismatch", "cells.by_id.cell_101.blueprint_priority_value", 4),
        ("cell_material_mismatch", "cells.by_id.cell_101.blueprint_material_tags", ["Iron"]),
        ("cell_progress_started", "cells.by_id.cell_101.blueprint_progress_percent", 1),
        ("cell_worker_present", "cells.by_id.cell_101.blueprint_worker_instance_id", 9),
        ("paused_required", "game.paused", False),
        ("sandbox_rejected", "game.sandbox_mode", True),
        ("source_priority_invalid", f"{INSTANCE}.priority_value", 10),
    ]
    for name, path, value in changes:
        candidate = fixture()
        write_path(candidate, path, value)
        try:
            build_constructable_priority_hint(candidate, instance_id=42, priority_class="high", priority_value=3)
        except ValueError:
            results.append(name)
        else:
            raise AssertionError(f"{name} was not rejected")

    for name, mutate in [
        ("missing_progress_unknown", lambda s: s["blueprints"]["by_instance_id"]["id_42"].pop("progress_percent")),
        ("missing_priority", lambda s: s["blueprints"]["by_instance_id"]["id_42"].pop("priority_class")),
        ("missing_quality", lambda s: s["quality"].pop(f"{INSTANCE}.worker_instance_id")),
        ("missing_footprint_cell", lambda s: s["cells"]["by_id"].pop("cell_101")),
    ]:
        candidate = fixture()
        mutate(candidate)
        try:
            build_constructable_priority_hint(candidate, instance_id=42, priority_class="high", priority_value=3)
        except ValueError:
            results.append(name)
        else:
            raise AssertionError(f"{name} was not rejected")
    for name, priority_class, priority_value in [
        ("invalid_target_class", "bogus", 3), ("nonstring_target_class", [], 3),
        ("invalid_target_value", "high", 10), ("boolean_target_value", "high", True),
        ("unchanged_target", "basic", 5),
    ]:
        try:
            build_constructable_priority_hint(fixture(), instance_id=42, priority_class=priority_class, priority_value=priority_value)
        except ValueError:
            results.append(name)
        else:
            raise AssertionError(f"{name} was not rejected")
    report = {
        "schema": "oni.constructable-priority-hint-verification.v1", "status": "passed",
        "case_count": len(results), "live_admission": False, "action_allowlisted": False,
        "reader_or_builder_mutation": False, "results": results,
        "limitations": ["observed-zero-progress subset only; missing native -1 sentinel requires future native witness", "no native setter or live effect"],
    }
    out = ROOT / "reports/constructable-priority-hint-verification.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
