#!/usr/bin/env python3
"""Offline regression checks for the additive v2 power telemetry overlay."""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
from typing import Any, Dict, Mapping


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import state_quality, strategy
from tools.game_verify import verify_power_telemetry_contract_v2 as v2
from tools.game_verify import verify_telemetry_contract as v1


CONTRACT_V1 = ROOT / "tools" / "game_verify" / "contracts" / "oni_telemetry.v1.json"
CONTRACT_V2 = ROOT / "tools" / "game_verify" / "contracts" / "oni_power_telemetry.v2.json"
SCENARIO = ROOT / "tools" / "game_verify" / "scenarios" / "oni_telemetry_contract_mock.json"
NOW = dt.datetime.fromisoformat("2026-08-30T08:00:01+08:00")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def objective(result: Mapping[str, Any], objective_id: str) -> Mapping[str, Any]:
    return next(item for item in result["objectives"] if item["id"] == objective_id)


def v1_blockers(snapshot: Dict[str, Any]) -> list[str]:
    contract = v1.load_json(CONTRACT_V1)
    v1.validate_contract_definition(contract, CONTRACT_V1)
    return v1.validate_snapshot(snapshot, v1.contract_units(contract), contract, now=NOW)


def quality_entry(contract: Mapping[str, Any], field: str, status: str) -> Dict[str, Any]:
    spec = contract["quality_contracts"][field]
    completeness = (
        contract["blocked_quality"][status]
        if status in contract["blocked_quality"]
        else spec["completeness"]
    )
    return {
        "status": status,
        "source": spec["source"],
        "semantic": spec["semantic"],
        "unit": spec["unit"],
        "scope": spec["scope"],
        "completeness": completeness,
        "captured_at": spec["captured_at"],
    }


def bounded_snapshot(base: Dict[str, Any], contract: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = copy.deepcopy(base)
    snapshot["power"]["storage_mode"] = "bounded"
    for field in ("storage_mode", "stored_joules", "capacity_joules", "battery_percent"):
        snapshot["quality"][f"power.{field}"] = quality_entry(contract, field, "observed")
    return snapshot


def unbounded_snapshot(bounded: Dict[str, Any], contract: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = copy.deepcopy(bounded)
    snapshot["power"].update(
        {
            "storage_mode": "unbounded",
            "stored_joules": 450000.0,
            "capacity_joules": None,
            "battery_percent": None,
        }
    )
    snapshot["quality"]["power.capacity_joules"] = quality_entry(
        contract, "capacity_joules", "unsupported"
    )
    snapshot["quality"]["power.battery_percent"] = quality_entry(
        contract, "battery_percent", "unsupported"
    )
    return snapshot


def zero_capacity_snapshot(bounded: Dict[str, Any], contract: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = copy.deepcopy(bounded)
    snapshot["power"].update(
        {
            "stored_joules": 0.0,
            "capacity_joules": 0.0,
            "battery_percent": None,
        }
    )
    snapshot["quality"]["power.battery_percent"] = quality_entry(
        contract, "battery_percent", "unsupported"
    )
    return snapshot


def invalid_capacity_snapshot(bounded: Dict[str, Any], contract: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = copy.deepcopy(bounded)
    snapshot["power"].update(
        {
            "storage_mode": "invalid",
            "stored_joules": 450000.0,
            "capacity_joules": None,
            "battery_percent": None,
        }
    )
    snapshot["quality"]["power.storage_mode"] = quality_entry(
        contract, "storage_mode", "error"
    )
    snapshot["quality"]["power.capacity_joules"] = quality_entry(
        contract, "capacity_joules", "error"
    )
    snapshot["quality"]["power.battery_percent"] = quality_entry(
        contract, "battery_percent", "error"
    )
    return snapshot


def require_strategy_fail_closed(snapshot: Dict[str, Any], label: str) -> None:
    candidate = copy.deepcopy(snapshot)
    candidate["bridge"] = {
        "stale": False,
        "trust_ready": True,
        "trust_blockers": [],
        "replay_mode": False,
    }
    result = strategy.evaluate_strategy(candidate, now=NOW)
    power = objective(result, "power_resilience")
    require(power["status"] == "unknown", f"{label}: missing percent entered the power decision")
    require(power["recommended_actions"] == [], f"{label}: power recommendation was generated")
    require(
        not any(str(item.get("id", "")).startswith("power_resilience") for item in result["next_actions"]),
        f"{label}: top-level power recommendation escaped the objective gate",
    )


def require_strategy_percent_rejected(snapshot: Dict[str, Any], label: str) -> None:
    candidate = copy.deepcopy(snapshot)
    candidate["bridge"] = {
        "stale": False,
        "trust_ready": True,
        "trust_blockers": [],
        "replay_mode": False,
    }
    metrics = strategy.collect_metrics(candidate)
    require(metrics["battery_percent"] is None, f"{label}: invalid v2 percent entered metrics")
    require(
        metrics["qualities"]["battery"] == "contract_mismatch",
        f"{label}: invalid v2 tuple was not labelled as a contract mismatch",
    )
    result = strategy.evaluate_strategy(candidate, now=NOW)
    power = objective(result, "power_resilience")
    require(power["status"] == "unknown", f"{label}: invalid v2 percent entered the power decision")
    require(power["recommended_actions"] == [], f"{label}: power recommendation was generated")
    require(
        "power.battery_percent" in result["telemetry"]["contract_blockers"],
        f"{label}: v2 blocker diagnostics were omitted",
    )


def main() -> int:
    contract = v2.load_json(CONTRACT_V2)
    v2.validate_contract_definition(contract, CONTRACT_V2)
    scenario = v1.load_json(SCENARIO)
    base = scenario["telemetry_contract"]["base_snapshot"]
    require(isinstance(base, dict), "v1 base snapshot is missing")

    bounded = bounded_snapshot(base, contract)
    bounded_before = copy.deepcopy(bounded)
    bounded_report = v2.verify_snapshot(bounded, contract_path=CONTRACT_V2, now=NOW)
    require(bounded_report["accepted"] is True, f"bounded v2 was rejected: {bounded_report['blockers']}")
    require(
        bounded_report["percent_dependent_actions_eligible"] is True,
        "bounded positive-capacity percent did not become action-eligible",
    )
    bounded_candidate = copy.deepcopy(bounded)
    bounded_candidate["bridge"] = {
        "stale": False,
        "trust_ready": True,
        "trust_blockers": [],
        "replay_mode": False,
    }
    bounded_metrics = strategy.collect_metrics(bounded_candidate)
    require(
        bounded_metrics["battery_percent"] == bounded["power"]["battery_percent"]
        and bounded_metrics["qualities"]["battery"] == "observed",
        "strategy rejected a valid bounded v2 power tuple",
    )
    require(v1_blockers(bounded) == [], "additive storage_mode broke the immutable v1 bounded contract")
    require(bounded == bounded_before, "v2 verifier mutated the bounded input")

    quantized_rounding_seam = copy.deepcopy(bounded)
    quantized_rounding_seam["power"].update(
        {
            "stored_joules": 1.0,
            "capacity_joules": 3.0,
            "battery_percent": 33.333,
        }
    )
    quantized_report = v2.verify_snapshot(
        quantized_rounding_seam,
        contract_path=CONTRACT_V2,
        now=NOW,
    )
    require(
        quantized_report["accepted"] is True,
        f"wire-quantized producer tuple was rejected: {quantized_report['blockers']}",
    )
    raw_derived_rounding_seam = copy.deepcopy(quantized_rounding_seam)
    raw_derived_rounding_seam["power"]["battery_percent"] = 33.342
    raw_derived_report = v2.verify_snapshot(
        raw_derived_rounding_seam,
        contract_path=CONTRACT_V2,
        now=NOW,
    )
    require(
        raw_derived_report["accepted"] is False and
        "power_battery_percent_inconsistent" in raw_derived_report["blockers"],
        "percent derived from pre-quantized gauges crossed the v2 wire seam",
    )

    unbounded = unbounded_snapshot(bounded, contract)
    unbounded_report = v2.verify_snapshot(unbounded, contract_path=CONTRACT_V2, now=NOW)
    require(unbounded_report["accepted"] is True, f"unbounded v2 was rejected: {unbounded_report['blockers']}")
    require(
        unbounded_report["percent_dependent_actions_eligible"] is False,
        "unbounded storage became percent-actionable",
    )
    require(
        "battery_percent_unavailable:unbounded_storage" in unbounded_report["action_blockers"],
        "unbounded action blocker was omitted",
    )
    old_unbounded_blockers = v1_blockers(unbounded)
    require(
        "type_invalid:power.capacity_joules" in old_unbounded_blockers
        and "type_invalid:power.battery_percent" in old_unbounded_blockers,
        "immutable v1 stopped rejecting nullable unbounded telemetry",
    )
    require(
        state_quality.snapshot_trust_blockers(unbounded, now=NOW) == [],
        "additive unbounded fields weakened or polluted Bridge transport admission",
    )
    require_strategy_fail_closed(unbounded, "unbounded")

    unbounded_error = copy.deepcopy(unbounded)
    unbounded_error["quality"]["power.capacity_joules"] = quality_entry(
        contract, "capacity_joules", "error"
    )
    unbounded_error["quality"]["power.battery_percent"] = quality_entry(
        contract, "battery_percent", "error"
    )
    unbounded_error_report = v2.verify_snapshot(
        unbounded_error, contract_path=CONTRACT_V2, now=NOW
    )
    require(
        unbounded_error_report["accepted"] is True
        and unbounded_error_report["percent_dependent_actions_eligible"] is False,
        "error/unknown unbounded transition state did not remain valid and fail-closed",
    )

    zero = zero_capacity_snapshot(bounded, contract)
    zero_report = v2.verify_snapshot(zero, contract_path=CONTRACT_V2, now=NOW)
    require(zero_report["accepted"] is True, f"zero-capacity v2 was rejected: {zero_report['blockers']}")
    require(zero_report["percent_dependent_actions_eligible"] is False, "zero capacity became percent-actionable")
    require(
        "type_invalid:power.battery_percent" in v1_blockers(zero),
        "immutable v1 stopped rejecting nullable zero-capacity percent",
    )
    require_strategy_fail_closed(zero, "zero_capacity")

    invalid = invalid_capacity_snapshot(bounded, contract)
    invalid_report = v2.verify_snapshot(invalid, contract_path=CONTRACT_V2, now=NOW)
    require(invalid_report["accepted"] is False, "invalid producer mode was accepted by v2")
    require(
        "power_storage_mode_invalid" in invalid_report["blockers"],
        "invalid producer mode blocker was omitted",
    )
    require(
        invalid_report["percent_dependent_actions_eligible"] is False,
        "invalid producer mode became percent-actionable",
    )
    require_strategy_fail_closed(invalid, "invalid_capacity")

    forged_percent = copy.deepcopy(unbounded)
    forged_percent["power"]["battery_percent"] = 10.0
    forged_percent["quality"]["power.battery_percent"] = quality_entry(
        contract, "battery_percent", "observed"
    )
    forged_report = v2.verify_snapshot(forged_percent, contract_path=CONTRACT_V2, now=NOW)
    require(forged_report["accepted"] is False, "unbounded finite percent was accepted")
    require(
        "power_unbounded_percent_must_be_null" in forged_report["blockers"],
        "unbounded percent forgery blocker was omitted",
    )
    require_strategy_percent_rejected(forged_percent, "unbounded_finite_percent")

    forged_capacity = copy.deepcopy(unbounded)
    forged_capacity["power"]["capacity_joules"] = 600000.0
    forged_report = v2.verify_snapshot(forged_capacity, contract_path=CONTRACT_V2, now=NOW)
    require(forged_report["accepted"] is False, "unbounded finite capacity was accepted")
    require(
        "power_unbounded_capacity_must_be_null" in forged_report["blockers"],
        "unbounded capacity forgery blocker was omitted",
    )

    forged_quality = copy.deepcopy(unbounded)
    forged_quality["quality"]["power.battery_percent"] = quality_entry(
        contract, "battery_percent", "observed"
    )
    forged_report = v2.verify_snapshot(forged_quality, contract_path=CONTRACT_V2, now=NOW)
    require(forged_report["accepted"] is False, "observed quality certified a null percent")
    require(
        "quality_status_invalid:power.battery_percent" in forged_report["blockers"],
        "null-percent quality mismatch blocker was omitted",
    )

    inconsistent = copy.deepcopy(bounded)
    inconsistent["power"]["battery_percent"] = 99.0
    inconsistent_report = v2.verify_snapshot(inconsistent, contract_path=CONTRACT_V2, now=NOW)
    require(inconsistent_report["accepted"] is False, "inconsistent bounded percent was accepted")
    require(
        "power_battery_percent_inconsistent" in inconsistent_report["blockers"],
        "bounded percent consistency blocker was omitted",
    )
    require_strategy_percent_rejected(inconsistent, "inconsistent_bounded_percent")

    missing_mode = copy.deepcopy(bounded)
    del missing_mode["power"]["storage_mode"]
    missing_mode_report = v2.verify_snapshot(missing_mode, contract_path=CONTRACT_V2, now=NOW)
    require(missing_mode_report["accepted"] is False, "missing storage_mode was accepted")
    require(
        "power_storage_mode_invalid" in missing_mode_report["blockers"],
        "missing storage mode blocker was omitted",
    )
    require_strategy_percent_rejected(missing_mode, "missing_storage_mode")

    huge_integer = copy.deepcopy(bounded)
    huge_integer["power"]["stored_joules"] = 10 ** 309
    require(
        v1.exact_number(huge_integer["power"]["stored_joules"]) is False,
        "v1 numeric admission accepted an integer too large for a finite float",
    )
    huge_v1_blockers = v1_blockers(huge_integer)
    require(
        "type_invalid:power.stored_joules" in huge_v1_blockers,
        "v1 did not reject an oversized JSON integer",
    )
    huge_report = v2.verify_snapshot(
        huge_integer, contract_path=CONTRACT_V2, now=NOW
    )
    require(huge_report["accepted"] is False, "v2 accepted an oversized JSON integer")
    require(
        "power_stored_joules_invalid" in huge_report["blockers"],
        "v2 oversized-integer blocker was omitted",
    )
    require_strategy_percent_rejected(huge_integer, "huge_integer")

    huge_integer_field = copy.deepcopy(bounded)
    huge_integer_field["cycle"] = 10 ** 309
    huge_integer_v1_blockers = v1_blockers(huge_integer_field)
    require(
        "range_invalid:cycle" in huge_integer_v1_blockers,
        "v1 skipped the range check for an oversized integer field",
    )
    huge_integer_field_report = v2.verify_snapshot(
        huge_integer_field, contract_path=CONTRACT_V2, now=NOW
    )
    require(
        huge_integer_field_report["accepted"] is False
        and "range_invalid:cycle" in huge_integer_field_report["blockers"],
        "v2 accepted an oversized integer inherited from the base contract",
    )

    huge_interval = copy.deepcopy(bounded)
    huge_interval["sample"]["interval_seconds"] = 10 ** 309
    require(
        "sample_interval_invalid"
        in state_quality.snapshot_trust_blockers(huge_interval, now=NOW),
        "Bridge admission did not fail closed on an oversized sample interval",
    )

    with tempfile.TemporaryDirectory(prefix="oni-power-contract-v2-") as temporary:
        nonfinite_path = pathlib.Path(temporary) / "nonfinite.json"
        nonfinite_path.write_text('{"power":{"capacity_joules":Infinity}}\n', encoding="utf-8")
        try:
            v2.load_json(nonfinite_path)
        except v2.PowerContractError:
            pass
        else:
            raise AssertionError("non-standard Infinity crossed the strict JSON boundary")

    print(
        json.dumps(
            {
                "status": "passed",
                "bounded_v1_compatible": True,
                "published_rounding_seam_bound": True,
                "unbounded_v2_accepted": True,
                "unbounded_error_quality_accepted": True,
                "unbounded_v1_rejected": True,
                "zero_capacity_fail_closed": True,
                "invalid_producer_rejected": True,
                "strategy_power_actions_fail_closed": True,
                "strategy_v2_percent_gate": True,
                "bridge_transport_admission_unchanged": True,
                "forgery_cases": 7,
                "huge_integer_fail_closed": True,
                "strict_json_nonfinite_rejected": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
