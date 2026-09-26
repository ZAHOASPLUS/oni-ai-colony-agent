#!/usr/bin/env python3
"""Offline verifier for completed-building occupancy telemetry.

This verifier exercises synthetic payloads against the Python field contract
and audits the source markers.  It never starts ONI, loads a save, or writes a
command, so a passing run remains offline evidence only.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.field_contracts import (  # noqa: E402
    FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID,
    FUNCTIONAL_BUILDING_OCCUPANCY_SCHEMA,
    contract_by_id,
    contract_quality_fixture,
    read_contract_field,
)

CONTRACT_PATH = ROOT / "tools/game_verify/contracts/oni_functional_building_occupancy.v1.json"
SCENARIO_PATH = ROOT / "tools/game_verify/scenarios/functional_building_occupancy_contract_v1.json"
SOURCE_PATH = ROOT / "mod/Source/Probe/FunctionalBuildingOccupancyTelemetryReader.cs"
BASE_TIME = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)


def _state(case: dict[str, Any]) -> dict[str, Any]:
    ts = BASE_TIME.isoformat()
    world_id = case["world_id"]
    active_world_id = case.get("active_world_id", world_id)
    return {
        "schema": "oni.state.v2",
        "session_id": "functional-building-occupancy-contract-v1",
        "seq": 1,
        "snapshot_seq": 1,
        "snapshot_id": "functional-building-occupancy-contract-v1:1",
        "ts": ts,
        "world_id": world_id,
        "observation_scope": {
            "active_world_id": active_world_id,
            "populated_world_ids": [active_world_id],
            "covered_world_ids": [active_world_id],
            "complete_for_populated_worlds": True,
        },
        "quality": {
            "functional_building_occupancy": contract_quality_fixture(
                FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID
            )
        },
        "functional_building_occupancy": {
            "schema": FUNCTIONAL_BUILDING_OCCUPANCY_SCHEMA,
            "captured_at": ts,
            "world_id": world_id,
            "complete": case["complete"],
            "instances": copy.deepcopy(case["instances"]),
        },
    }


def _check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}".rstrip())


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    source = SOURCE_PATH.read_text(encoding="utf-8")
    _check("contract schema", contract["payload_schema"] == FUNCTIONAL_BUILDING_OCCUPANCY_SCHEMA)
    _check("contract id", contract["quality_contract_id"] == FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID)
    _check("occupied-cell limit", contract["max_occupied_cells"] == 64)
    _check("offline read-only", scenario["live_admission"] is False and scenario["action_types"] == [])
    registered = contract_by_id(FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID)
    _check("registered contract", registered is not None)
    _check("non-mutating", registered.mutating_condition_allowed is False)
    for marker in (
        "Components.BuildingCompletes.GetWorldItems(activeWorldId)",
        "Structure",
        "IsEntombed()",
        "OccupyArea",
        "GetOccupiedGridCells()",
        "Grid.IsValidCellInWorld",
        "WorldBindingMatches(manager, activeWorld, activeWorldId)",
        "building_occupied_cell_duplicate",
        "building_occupancy_changed_during_read",
    ):
        _check(f"source marker: {marker}", marker in source)
    _check("placement cells are not occupancy source", "PlacementCells" not in source)
    _check("no action authority", "TryPlace" not in source and "ActionExecutor" not in source)

    results = []
    for case in scenario["cases"]:
        read = read_contract_field(
            _state(case),
            "functional_building_occupancy",
            FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID,
        )
        usable = bool(read.usable)
        expected = case["expected"] == "usable"
        _check(case["id"], usable is expected, f"usable={usable}, blockers={read.blockers}")
        results.append({"id": case["id"], "usable": usable, "blockers": list(read.blockers)})

    stale = _state(scenario["cases"][0])
    stale["functional_building_occupancy"]["captured_at"] = (
        BASE_TIME - dt.timedelta(seconds=6)
    ).isoformat()
    stale_read = read_contract_field(
        stale,
        "functional_building_occupancy",
        FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID,
    )
    _check(
        "stale fail closed",
        not stale_read.usable and "quality_capture_stale" in stale_read.blockers,
    )
    estimated = _state(scenario["cases"][0])
    estimated["quality"]["functional_building_occupancy"]["status"] = "estimated"
    estimated_read = read_contract_field(
        estimated,
        "functional_building_occupancy",
        FUNCTIONAL_BUILDING_OCCUPANCY_CONTRACT_ID,
    )
    _check("estimated fail closed", not estimated_read.usable)
    results.extend(
        [
            {"id": "stale_capture", "usable": bool(stale_read.usable), "blockers": list(stale_read.blockers)},
            {"id": "estimated_quality", "usable": bool(estimated_read.usable), "blockers": list(estimated_read.blockers)},
        ]
    )

    report = {
        "schema": "oni.functional-building-occupancy-verification.v1",
        "status": "passed",
        "live_admission": False,
        "case_count": len(results),
        "results": results,
        "contract_sha256": hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
        "scenario_sha256": hashlib.sha256(SCENARIO_PATH.read_bytes()).hexdigest(),
        "source": str(SOURCE_PATH.resolve()),
        "source_sha256": hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest(),
    }
    report_path = ROOT / "reports" / "functional-building-occupancy-verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
