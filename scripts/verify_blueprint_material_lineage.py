#!/usr/bin/env python3
"""Offline verifier for the read-only blueprint material-lineage envelope."""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.field_contracts import (  # noqa: E402
    BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID,
    contract_quality_fixture,
    read_contract_field,
)

CONTRACT_PATH = ROOT / "tools/game_verify/contracts/oni_blueprint_material_lineage.v1.json"
SCENARIO_PATH = ROOT / "tools/game_verify/scenarios/blueprint_material_lineage_contract_v1.json"
SOURCE_PATH = ROOT / "mod/Source/Probe/ProbeStateReader.cs"
TS = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc).isoformat()


def _state(path: str, value: object) -> dict:
    payload: dict = {}
    cursor = payload
    parts = path.split(".")
    for part in parts[:-1]:
        child: dict = {}
        cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value
    return {
        "schema": "oni.state.v2",
        "seq": 1,
        "snapshot_seq": 1,
        "snapshot_id": "material-lineage-contract:1",
        "ts": TS,
        "quality": {path: contract_quality_fixture(BLUEPRINT_MATERIAL_LINEAGE_CONTRACT_ID)},
        **payload,
    }


def _item(tag: str = "SandStone", required: object = 10.0, delivered: object = 4.0, remaining: object = 6.0, status: str = "pending") -> dict:
    return {
        "tag": tag,
        "required_mass_kg": required,
        "delivered_mass_kg": delivered,
        "remaining_mass_kg": remaining,
        "delivery_status": status,
    }


def _check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}".rstrip())


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    _check("contract schema", contract["payload_schema"] == "oni.blueprint_material_lineage.v1")
    _check("contract is read-only", contract["mutating_condition_allowed"] is False and contract["safety"]["actions"] == [])
    _check("scenario is offline", scenario["live_admission"] is False and scenario["action_types"] == [])
    source = SOURCE_PATH.read_text(encoding="utf-8")
    helper = re.search(r"private static bool TryReadBlueprintMaterialLineage\(.*?\n        }", source, re.DOTALL)
    _check("producer helper present", helper is not None)
    helper_text = helper.group(0)
    for marker in (
        "GetAllIngredients",
        "GetMassAvailable",
        "ReadActiveWorldId() == worldId",
        "expectedInstanceId",
        "string.IsNullOrEmpty(ingredientTag)",
        "delivery_status",
    ):
        _check(f"producer marker: {marker}", marker in helper_text)
    _check("dynamic lineage excluded from fingerprint", "blueprint_material_lineage" not in source[source.find("BlueprintCellFingerprint"):source.find("BlueprintCellFingerprint") + 2000])

    cell_path = "cells.by_id.cell_1.blueprint_material_lineage"
    instance_path = "blueprints.by_instance_id.id_42.material_lineage"
    valid = [_item(delivered=10.0, remaining=0.0, status="complete")]
    cases = [
        ("complete_delivery", cell_path, valid, True),
        ("partial_delivery", cell_path, [_item()], True),
        ("storage_missing_unknown", cell_path, [_item(delivered=None, remaining=None, status="unknown")], True),
        ("instance_binding", instance_path, valid, True),
        ("duplicate_tag_rejected", cell_path, [_item(), _item()], False),
        ("nonfinite_mass_rejected", cell_path, [_item(required=float("nan"))], False),
        ("oversized_list_rejected", cell_path, [_item()] * 9, False),
        ("unknown_with_mass_rejected", cell_path, [_item(status="unknown")], False),
        ("oversupply_rejected", cell_path, [_item(delivered=11.0, remaining=0.0, status="complete")], False),
        ("mass_not_conserved_rejected", cell_path, [_item(delivered=4.0, remaining=5.0, status="pending")], False),
    ]
    results = []
    for label, path, value, expected in cases:
        read = read_contract_field(_state(path, value), path)
        _check(label, bool(read.usable) is expected, f"usable={read.usable}, blockers={read.blockers}")
        results.append({"id": label, "usable": bool(read.usable), "blockers": list(read.blockers)})
    print({"cases": len(results), "passed": len(results), "results": results, "reservation_not_delivery": True, "related_world_not_delivery": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
