#!/usr/bin/env python3
"""Offline verifier for additive completed-building utility_network telemetry.

The cases are synthetic contract checks only.  This script never starts
ONI/Steam/Bridge, loads a save, writes a command, or grants live admission.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.field_contracts import (  # noqa: E402
    FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID,
    FUNCTIONAL_BUILDING_UTILITY_NETWORK_SCHEMA,
    FUNCTIONAL_BUILDING_UTILITY_NETWORK_SOURCE,
    contract_by_id,
    contract_quality_fixture,
    read_contract_field,
)

CONTRACT_PATH = ROOT / "tools/game_verify/contracts/oni_functional_building_utility_network.v1.json"
SCENARIO_PATH = ROOT / "tools/game_verify/scenarios/functional_building_utility_network_contract_v1.json"
SOURCE_PATH = ROOT / "mod/Source/Probe/FunctionalBuildingUtilityNetworkTelemetryReader.cs"
BASE_TIME = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)


def _state(case: dict[str, Any]) -> dict[str, Any]:
    ts = BASE_TIME.isoformat()
    world_id = case["world_id"]
    active_world_id = case.get("active_world_id", world_id)
    return {
        "schema": "oni.state.v2",
        "session_id": "functional-building-utility_network-contract-v1",
        "seq": 1,
        "snapshot_seq": 1,
        "snapshot_id": "functional-building-utility_network-contract-v1:1",
        "ts": ts,
        "world_id": world_id,
        "observation_scope": {
            "active_world_id": active_world_id,
            "populated_world_ids": [active_world_id],
            "covered_world_ids": [active_world_id],
            "complete_for_populated_worlds": True,
        },
        "quality": {
            "functional_building_utility_network": contract_quality_fixture(
                FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID
            )
        },
        "functional_building_utility_network": {
            "schema": FUNCTIONAL_BUILDING_UTILITY_NETWORK_SCHEMA,
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
    _check("contract schema", contract["payload_schema"] == FUNCTIONAL_BUILDING_UTILITY_NETWORK_SCHEMA)
    _check("contract id", contract["quality_contract_id"] == FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID)
    _check("contract source", contract["source"] == FUNCTIONAL_BUILDING_UTILITY_NETWORK_SOURCE)
    _check("offline read-only", scenario["live_admission"] is False and scenario["action_types"] == [])
    _check("no mutation calls", all(marker not in source for marker in (".Connect(", ".Disconnect(", "ForceRebuildNetworks")))
    registered = contract_by_id(FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID)
    _check("registered contract", registered is not None)
    _check("non-mutating", registered.mutating_condition_allowed is False)
    for marker in (
        "Components.BuildingCompletes.GetWorldItems(activeWorldId)",
        "GetNetworkForCell",
        "GetUtilityInputCell",
        "GetUtilityOutputCell",
        "solidConduitSystem",
        "Grid.IsValidCellInWorld",
        "WorldBindingMatches(manager, activeWorld, activeWorldId)",
        "building_utility_network_changed_during_read",
    ):
        _check(f"source marker: {marker}", marker in source)

    results = []
    for case in scenario["cases"]:
        read = read_contract_field(
            _state(case), "functional_building_utility_network", FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID
        )
        usable = bool(read.usable)
        expected = case["expected"] == "usable"
        _check(case["id"], usable is expected, f"usable={usable}, blockers={read.blockers}")
        results.append({"id": case["id"], "usable": usable, "blockers": list(read.blockers)})

    stale = _state(scenario["cases"][0])
    stale["functional_building_utility_network"]["captured_at"] = (
        BASE_TIME - dt.timedelta(seconds=6)
    ).isoformat()
    stale_read = read_contract_field(
        stale, "functional_building_utility_network", FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID
    )
    _check("stale fail closed", not stale_read.usable and "quality_capture_stale" in stale_read.blockers)
    estimated = _state(scenario["cases"][0])
    estimated["quality"]["functional_building_utility_network"]["status"] = "estimated"
    estimated_read = read_contract_field(
        estimated, "functional_building_utility_network", FUNCTIONAL_BUILDING_UTILITY_NETWORK_CONTRACT_ID
    )
    _check("estimated fail closed", not estimated_read.usable)
    results.extend(
        [
            {"id": "stale_capture", "usable": bool(stale_read.usable), "blockers": list(stale_read.blockers)},
            {"id": "estimated_quality", "usable": bool(estimated_read.usable), "blockers": list(estimated_read.blockers)},
        ]
    )

    report = {
        "schema": "oni.functional-building-utility-network-verification.v1",
        "status": "passed",
        "live_admission": False,
        "case_count": len(results),
        "results": results,
        "contract_sha256": hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
        "scenario_sha256": hashlib.sha256(SCENARIO_PATH.read_bytes()).hexdigest(),
        "source": str(SOURCE_PATH.resolve()),
        "source_sha256": hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest(),
    }
    report_path = ROOT / "reports/functional-building-utility-network-verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



