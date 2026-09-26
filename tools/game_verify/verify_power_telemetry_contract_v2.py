#!/usr/bin/env python3
"""Validate the additive, fail-closed v2 power telemetry overlay.

The transport envelope remains ``oni.state.v2``.  This verifier reuses every
field from the immutable v1 telemetry contract except the three power storage
fields that v2 deliberately replaces with a mode-aware nullable contract.
It never mutates or default-fills the supplied snapshot.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import pathlib
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT_PATH = pathlib.Path(__file__).resolve().parent / "contracts" / "oni_power_telemetry.v2.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.game_verify import verify_telemetry_contract as v1


VERIFICATION_SCHEMA = "oni.power-telemetry-verification.v2"
EXPECTED_REPLACED_FIELDS = (
    "power.stored_joules",
    "power.capacity_joules",
    "power.battery_percent",
)
_MISSING = object()


class PowerContractError(ValueError):
    """Raised when the v2 overlay itself is malformed."""


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    try:
        return v1.load_json(path)
    except v1.ContractError as error:
        raise PowerContractError(str(error)) from error


def exact_number(value: Any) -> bool:
    return v1.exact_number(value)


def parse_now(value: Optional[str]) -> dt.datetime:
    current = dt.datetime.now(dt.timezone.utc).astimezone() if value is None else v1.parse_aware_timestamp(value, "now")
    return current


def validate_contract_definition(contract: Mapping[str, Any], path: pathlib.Path) -> None:
    if contract.get("schema") != "oni.power-telemetry-contract.v2" or contract.get("version") != 2:
        raise PowerContractError(f"{path} has an unsupported power contract version")
    if contract.get("snapshot") != {"schema": "oni.state.v2", "event": "state_snapshot"}:
        raise PowerContractError("v2 power contract must remain an additive oni.state.v2 overlay")

    extends = contract.get("extends")
    if not isinstance(extends, Mapping):
        raise PowerContractError("extends must be an object")
    if extends.get("path") != "oni_telemetry.v1.json" or extends.get("schema") != "oni.telemetry-contract.v1":
        raise PowerContractError("v2 power contract must explicitly extend immutable telemetry v1")
    if tuple(extends.get("replaced_fields") or ()) != EXPECTED_REPLACED_FIELDS:
        raise PowerContractError("v2 may replace only the three power storage fields")

    limits = contract.get("limits")
    maximum = limits.get("maximum_joules") if isinstance(limits, Mapping) else None
    tolerance = limits.get("percent_tolerance") if isinstance(limits, Mapping) else None
    if not exact_number(maximum) or float(maximum) <= 0.0:
        raise PowerContractError("limits.maximum_joules must be a positive finite number")
    if not exact_number(tolerance) or not (0.0 < float(tolerance) <= 0.01):
        raise PowerContractError("limits.percent_tolerance must be finite and fail-closed")
    if contract.get("storage_modes") != ["bounded", "unbounded"]:
        raise PowerContractError("storage_modes must be exactly bounded and unbounded")
    if contract.get("blocked_quality") != {"unsupported": "complete", "error": "unknown"}:
        raise PowerContractError("blocked quality/status pairs drifted")

    quality = contract.get("quality_contracts")
    if not isinstance(quality, Mapping) or set(quality) != {
        "storage_mode",
        "stored_joules",
        "capacity_joules",
        "battery_percent",
    }:
        raise PowerContractError("quality_contracts must cover exactly the v2 power fields")
    for name, spec in quality.items():
        if not isinstance(spec, Mapping):
            raise PowerContractError(f"quality contract {name} must be an object")
        for key in ("source", "semantic", "unit", "scope", "captured_at"):
            if not isinstance(spec.get(key), str) or not spec.get(key):
                raise PowerContractError(f"quality contract {name}.{key} is missing")
    gate = contract.get("action_gate")
    if not isinstance(gate, Mapping) or gate.get("name") != "percent_dependent_actions_eligible":
        raise PowerContractError("the percent-dependent action gate is missing")


def _load_base_contract(contract: Mapping[str, Any], contract_path: pathlib.Path) -> Tuple[Dict[str, Any], pathlib.Path]:
    base_path = (contract_path.parent / str(contract["extends"]["path"])).resolve()
    base = load_json(base_path)
    try:
        v1.validate_contract_definition(base, base_path)
    except v1.ContractError as error:
        raise PowerContractError(str(error)) from error
    if base.get("schema") != contract["extends"]["schema"]:
        raise PowerContractError("base telemetry contract schema does not match the v2 declaration")
    return base, base_path


def _base_without_replaced_fields(base: Mapping[str, Any]) -> Dict[str, Any]:
    filtered = copy.deepcopy(dict(base))
    filtered["fields"] = [
        field for field in filtered["fields"] if field.get("path") not in EXPECTED_REPLACED_FIELDS
    ]
    return filtered


def _quality_blockers(
    snapshot: Mapping[str, Any],
    contract: Mapping[str, Any],
    field: str,
    *,
    statuses: Sequence[str],
    completeness_by_status: Optional[Mapping[str, str]] = None,
) -> List[str]:
    blockers: List[str] = []
    entry = v1.get_path(snapshot, f"quality.power.{field}")
    spec = contract["quality_contracts"][field]
    if not isinstance(entry, Mapping):
        return [f"quality_missing:power.{field}"]
    status = entry.get("status")
    if status not in statuses:
        blockers.append(f"quality_status_invalid:power.{field}")
    for key in ("source", "semantic", "unit", "scope", "captured_at"):
        if entry.get(key) != spec[key]:
            blockers.append(f"quality_{key}_mismatch:power.{field}")
    expected_completeness = (
        completeness_by_status.get(str(status))
        if completeness_by_status is not None
        else spec.get("completeness")
    )
    if entry.get("completeness") != expected_completeness:
        blockers.append(f"quality_completeness_mismatch:power.{field}")
    return blockers


def validate_power_overlay(
    snapshot: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> Tuple[List[str], bool, List[str]]:
    blockers: List[str] = []
    action_blockers: List[str] = []
    power = snapshot.get("power")
    if not isinstance(power, Mapping):
        return ["power_missing"], False, ["power_telemetry_invalid"]

    mode = power.get("storage_mode", _MISSING)
    if mode not in contract["storage_modes"]:
        blockers.append("power_storage_mode_invalid")

    maximum = float(contract["limits"]["maximum_joules"])
    stored = power.get("stored_joules", _MISSING)
    if not exact_number(stored) or not (0.0 <= float(stored) <= maximum):
        blockers.append("power_stored_joules_invalid")
    blockers.extend(
        _quality_blockers(snapshot, contract, "storage_mode", statuses=("observed",))
    )
    blockers.extend(
        _quality_blockers(snapshot, contract, "stored_joules", statuses=("observed",))
    )

    capacity = power.get("capacity_joules", _MISSING)
    percent = power.get("battery_percent", _MISSING)
    blocked_pairs = contract["blocked_quality"]
    percent_actionable = False

    if mode == "bounded":
        if not exact_number(capacity) or not (0.0 <= float(capacity) <= maximum):
            blockers.append("power_capacity_joules_invalid")
        blockers.extend(
            _quality_blockers(snapshot, contract, "capacity_joules", statuses=("observed",))
        )
        if exact_number(stored) and exact_number(capacity) and float(stored) > float(capacity):
            blockers.append("power_stored_exceeds_capacity")

        if exact_number(capacity) and float(capacity) > 0.0:
            if not exact_number(percent) or not (0.0 <= float(percent) <= 100.0):
                blockers.append("power_battery_percent_invalid")
            elif exact_number(stored):
                expected = float(stored) / float(capacity) * 100.0
                if abs(float(percent) - expected) > float(contract["limits"]["percent_tolerance"]):
                    blockers.append("power_battery_percent_inconsistent")
            percent_quality_blockers = _quality_blockers(
                snapshot,
                contract,
                "battery_percent",
                statuses=("observed",),
            )
            blockers.extend(percent_quality_blockers)
            percent_actionable = not blockers and not percent_quality_blockers
        else:
            if percent is not None:
                blockers.append("power_zero_capacity_percent_must_be_null")
            blockers.extend(
                _quality_blockers(
                    snapshot,
                    contract,
                    "battery_percent",
                    statuses=("unsupported",),
                    completeness_by_status=blocked_pairs,
                )
            )
            action_blockers.append("battery_percent_unavailable:zero_capacity")
    elif mode == "unbounded":
        if capacity is not None:
            blockers.append("power_unbounded_capacity_must_be_null")
        if percent is not None:
            blockers.append("power_unbounded_percent_must_be_null")
        blockers.extend(
            _quality_blockers(
                snapshot,
                contract,
                "capacity_joules",
                statuses=tuple(blocked_pairs),
                completeness_by_status=blocked_pairs,
            )
        )
        blockers.extend(
            _quality_blockers(
                snapshot,
                contract,
                "battery_percent",
                statuses=tuple(blocked_pairs),
                completeness_by_status=blocked_pairs,
            )
        )
        action_blockers.append("battery_percent_unavailable:unbounded_storage")

    blockers = sorted(set(blockers))
    if blockers:
        percent_actionable = False
        action_blockers.append("power_telemetry_invalid")
    return blockers, percent_actionable, sorted(set(action_blockers))


def verify_snapshot(
    snapshot: Dict[str, Any],
    *,
    contract_path: pathlib.Path = DEFAULT_CONTRACT_PATH,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    contract_path = contract_path.resolve()
    contract = load_json(contract_path)
    validate_contract_definition(contract, contract_path)
    base, base_path = _load_base_contract(contract, contract_path)
    filtered_base = _base_without_replaced_fields(base)
    units = v1.contract_units(filtered_base)
    current = now or dt.datetime.now(dt.timezone.utc).astimezone()
    base_blockers = v1.validate_snapshot(snapshot, units, filtered_base, now=current)
    power_blockers, action_eligible, action_blockers = validate_power_overlay(snapshot, contract)
    blockers = sorted(set(base_blockers + power_blockers))
    accepted = not blockers
    if not accepted:
        action_eligible = False
        action_blockers = sorted(set(action_blockers + ["power_telemetry_invalid"]))
    return {
        "schema": VERIFICATION_SCHEMA,
        "accepted": accepted,
        "percent_dependent_actions_eligible": bool(accepted and action_eligible),
        "blockers": blockers,
        "action_blockers": action_blockers,
        "storage_mode": v1.get_path(snapshot, "power.storage_mode"),
        "contract": str(contract_path),
        "base_contract": str(base_path),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT_PATH))
    parser.add_argument("--now")
    parser.add_argument("--output")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        snapshot = load_json(pathlib.Path(args.snapshot).resolve())
        report = verify_snapshot(
            snapshot,
            contract_path=pathlib.Path(args.contract),
            now=parse_now(args.now),
        )
        exit_code = 0 if report["accepted"] else 1
    except (PowerContractError, v1.ContractError, OSError, ValueError, OverflowError) as error:
        report = {
            "schema": VERIFICATION_SCHEMA,
            "accepted": False,
            "percent_dependent_actions_eligible": False,
            "blockers": ["contract_error"],
            "action_blockers": ["power_telemetry_invalid"],
            "error": str(error),
        }
        exit_code = 2
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        pathlib.Path(args.output).resolve().write_text(text, encoding="utf-8")
    if args.json or not args.output:
        print(text, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
