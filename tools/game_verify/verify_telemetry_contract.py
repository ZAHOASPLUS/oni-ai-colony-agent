#!/usr/bin/env python3
"""Validate Mock scenarios or an unmodified live ONI telemetry snapshot.

The bundled game-mod-verify mock runner deliberately does not execute arbitrary
commands.  This validator therefore evaluates the custom
``telemetry_contract_check`` payload in each mock action, then proves that the
computed outcome is bound to that action's emitted event/state and to the
step's expect/evidence assertions.  The runner can subsequently exercise the
same scenario without gaining any game-control capability.

Live mode reads an explicitly supplied Probe/Bridge snapshot, optionally binds
it to Bridge health, and emits an audit report.  It never starts or controls
the game and never upgrades, mutates, or default-fills the snapshot.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import pathlib
import re
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO_DIR = pathlib.Path(__file__).resolve().parent / "scenarios"
DEFAULT_CONTRACT_PATH = pathlib.Path(__file__).resolve().parent / "contracts" / "oni_telemetry.v1.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import state_quality


ACCEPT_EVENT = "oni.telemetry.contract.accepted"
REJECT_EVENT = "oni.telemetry.contract.rejected"
KNOWN_FIELD_TYPES = {
    "array_integer",
    "array_string",
    "boolean",
    "integer",
    "nullable_string",
    "number",
    "string",
}
_MISSING = object()


class ContractError(ValueError):
    """A deterministic contract or scenario binding failure."""


def reject_nonstandard_constant(token: str) -> None:
    raise ContractError(f"non-standard JSON numeric constant is forbidden: {token}")


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=reject_nonstandard_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return payload


def require_path(path: str) -> List[str]:
    parts = path.split(".")
    if not path or any(not part for part in parts):
        raise ContractError(f"invalid dotted path: {path!r}")
    return parts


def existing_mapping_prefix(current: Mapping[str, Any], parts: Sequence[str]) -> tuple[str, int] | None:
    """Find the longest dotted key whose value can be traversed further.

    ONI quality entries intentionally use flat dotted keys such as
    ``resources.food_kcal`` while the snapshot payload itself uses nested
    objects.  Contract paths must address both representations without
    rewriting the production wire shape.
    """

    for length in range(len(parts) - 1, 0, -1):
        key = ".".join(parts[:length])
        if isinstance(current.get(key), Mapping):
            return key, length
    return None


def get_path(value: Mapping[str, Any], dotted_path: str, default: Any = _MISSING) -> Any:
    parts = require_path(dotted_path)
    current: Any = value
    while parts:
        if not isinstance(current, Mapping):
            return default
        exact = ".".join(parts)
        if exact in current:
            return current[exact]
        prefix = existing_mapping_prefix(current, parts)
        if prefix is None:
            return default
        key, length = prefix
        current = current[key]
        parts = parts[length:]
    return current


def set_path(value: Dict[str, Any], dotted_path: str, replacement: Any) -> None:
    parts = require_path(dotted_path)
    current: Dict[str, Any] = value
    while parts:
        exact = ".".join(parts)
        if exact in current or len(parts) == 1:
            current[exact] = replacement
            return
        prefix = existing_mapping_prefix(current, parts)
        if prefix is not None:
            key, length = prefix
            current = current[key]
            parts = parts[length:]
            continue
        child: Dict[str, Any] = {}
        current[parts[0]] = child
        current = child
        parts = parts[1:]


def remove_path(value: Dict[str, Any], dotted_path: str) -> None:
    parts = require_path(dotted_path)
    current: Dict[str, Any] = value
    while parts:
        exact = ".".join(parts)
        if exact in current:
            del current[exact]
            return
        prefix = existing_mapping_prefix(current, parts)
        if prefix is None:
            break
        key, length = prefix
        current = current[key]
        parts = parts[length:]
    raise ContractError(f"remove mutation target is missing: {dotted_path}")


def apply_mutations(snapshot: Dict[str, Any], mutations: Any) -> None:
    if mutations is None:
        return
    if not isinstance(mutations, list):
        raise ContractError("telemetry_contract_check.mutations must be an array")
    for index, mutation in enumerate(mutations):
        if not isinstance(mutation, dict):
            raise ContractError(f"mutation {index} must be an object")
        operation = mutation.get("op")
        path = mutation.get("path")
        if not isinstance(path, str):
            raise ContractError(f"mutation {index} path must be a string")
        if operation == "set":
            if "value" not in mutation:
                raise ContractError(f"set mutation {index} has no value")
            set_path(snapshot, path, copy.deepcopy(mutation["value"]))
        elif operation == "remove":
            remove_path(snapshot, path)
        else:
            raise ContractError(f"mutation {index} has unsupported op {operation!r}")


def parse_aware_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{label} must be a non-empty RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError(f"{label} is not a valid RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{label} must include a UTC offset")
    return parsed


def exact_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(numeric)


def field_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "number":
        return exact_number(value)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array_string":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if expected_type == "array_integer":
        return isinstance(value, list) and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    if expected_type == "nullable_string":
        return value is None or (isinstance(value, str) and bool(value.strip()))
    if expected_type == "string":
        return isinstance(value, str) and bool(value.strip())
    return False


def require_string_list(value: Any, label: str, *, nonempty: bool = False) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{label} must be an array of non-empty strings")
    if nonempty and not value:
        raise ContractError(f"{label} must not be empty")
    return list(value)


def validate_contract_definition(contract: Dict[str, Any], path: pathlib.Path) -> None:
    if contract.get("schema") != "oni.telemetry-contract.v1":
        raise ContractError(f"{path} has unsupported contract schema")
    snapshot = contract.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "oni.state.v2" or snapshot.get("event") != "state_snapshot":
        raise ContractError(f"{path} does not bind oni.state.v2/state_snapshot")

    freshness = contract.get("freshness")
    expected_freshness = {
        "timestamp_path": "ts",
        "sample_interval_path": "sample.interval_seconds",
        "minimum_interval_seconds": state_quality.MIN_TRUSTED_SAMPLE_INTERVAL_SECONDS,
        "maximum_interval_seconds": state_quality.MAX_TRUSTED_SAMPLE_INTERVAL_SECONDS,
        "maximum_future_skew_seconds": state_quality.MAX_FUTURE_CLOCK_SKEW_SECONDS,
        "stale_after_intervals": 3.0,
    }
    if freshness != expected_freshness:
        raise ContractError("contract freshness limits drift from agent.state_quality")

    quality = contract.get("quality")
    allowed_statuses = require_string_list(
        quality.get("allowed_statuses") if isinstance(quality, dict) else None,
        "quality.allowed_statuses",
        nonempty=True,
    )
    if set(allowed_statuses) != state_quality.USABLE_STATUSES | state_quality.BLOCKED_STATUSES:
        raise ContractError("contract quality statuses drift from agent.state_quality")

    expected_scope_policy = {
        "coverage_mode": "single_active_world",
        "covered_world_ids": "exactly_active_world_id",
        "complete_populated_world_ids": "exactly_active_world_id",
    }
    if contract.get("observation_scope_policy") != expected_scope_policy:
        raise ContractError("observation scope policy must bind v1 to one active world")

    required_paths = require_string_list(contract.get("required_paths"), "required_paths", nonempty=True)
    if len(required_paths) != len(set(required_paths)):
        raise ContractError("required_paths contains duplicates")
    for required in required_paths:
        require_path(required)

    fields = contract.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ContractError("fields must be a non-empty array")
    seen: set[str] = set()
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            raise ContractError(f"fields[{index}] must be an object")
        field_path = field.get("path")
        if not isinstance(field_path, str):
            raise ContractError(f"fields[{index}].path must be a string")
        require_path(field_path)
        if field_path in seen:
            raise ContractError(f"duplicate field contract: {field_path}")
        seen.add(field_path)
        if field.get("type") not in KNOWN_FIELD_TYPES:
            raise ContractError(f"unsupported type for {field_path}: {field.get('type')!r}")
        if not isinstance(field.get("unit"), str) or not field["unit"]:
            raise ContractError(f"field {field_path} has no explicit unit")
        minimum = field.get("minimum", _MISSING)
        maximum = field.get("maximum", _MISSING)
        if minimum is not _MISSING and not exact_number(minimum):
            raise ContractError(f"field {field_path} minimum is not finite numeric")
        if maximum is not _MISSING and not exact_number(maximum):
            raise ContractError(f"field {field_path} maximum is not finite numeric")
        if minimum is not _MISSING and maximum is not _MISSING and minimum > maximum:
            raise ContractError(f"field {field_path} minimum exceeds maximum")
        allowed_values = field.get("allowed_values")
        if allowed_values is not None:
            if not isinstance(allowed_values, list) or not allowed_values:
                raise ContractError(f"field {field_path} allowed_values must be a non-empty array")
            if any(not field_type_matches(item, field["type"]) for item in allowed_values):
                raise ContractError(f"field {field_path} allowed_values do not match its type")
            if len({json.dumps(item, sort_keys=True) for item in allowed_values}) != len(allowed_values):
                raise ContractError(f"field {field_path} allowed_values contains duplicates")
        maximum_items = field.get("maximum_items")
        unique_items = field.get("unique_items")
        sorted_items = field.get("sorted_items")
        item_pattern = field.get("item_pattern")
        allowed_items = field.get("allowed_items")
        has_array_rules = any(
            value is not None
            for value in (maximum_items, unique_items, sorted_items, item_pattern, allowed_items)
        )
        if has_array_rules and field["type"] not in {"array_string", "array_integer"}:
            raise ContractError(f"field {field_path} has array rules for a non-array type")
        if maximum_items is not None and (
            isinstance(maximum_items, bool) or not isinstance(maximum_items, int) or maximum_items < 0
        ):
            raise ContractError(f"field {field_path} maximum_items must be a non-negative integer")
        for rule_name, rule_value in (("unique_items", unique_items), ("sorted_items", sorted_items)):
            if rule_value is not None and not isinstance(rule_value, bool):
                raise ContractError(f"field {field_path} {rule_name} must be boolean")
        if item_pattern is not None:
            if field["type"] != "array_string" or not isinstance(item_pattern, str) or not item_pattern:
                raise ContractError(f"field {field_path} item_pattern requires a non-empty string array pattern")
            try:
                re.compile(item_pattern)
            except re.error as error:
                raise ContractError(f"field {field_path} item_pattern is invalid: {error}") from error
        if allowed_items is not None:
            if not isinstance(allowed_items, list) or not allowed_items:
                raise ContractError(f"field {field_path} allowed_items must be a non-empty array")
            expected_item_type = str if field["type"] == "array_string" else int
            if any(
                isinstance(item, bool) or not isinstance(item, expected_item_type)
                for item in allowed_items
            ):
                raise ContractError(f"field {field_path} allowed_items do not match its array item type")
            if len(allowed_items) != len(set(allowed_items)):
                raise ContractError(f"field {field_path} allowed_items contains duplicates")
            if item_pattern is not None and any(re.fullmatch(item_pattern, item) is None for item in allowed_items):
                raise ContractError(f"field {field_path} allowed_items violate item_pattern")
        if field.get("quality_required") is True:
            actionable = require_string_list(
                field.get("actionable_quality"),
                f"fields[{index}].actionable_quality",
                nonempty=True,
            )
            if not set(actionable).issubset(state_quality.USABLE_STATUSES):
                raise ContractError(f"field {field_path} permits non-usable actionable quality")
        quality_contract = field.get("quality_contract")
        if quality_contract is not None:
            if field.get("quality_required") is not True:
                raise ContractError(f"field {field_path} has metadata contract without required quality")
            if not isinstance(quality_contract, dict):
                raise ContractError(f"field {field_path} quality_contract must be an object")
            statuses = quality_contract.get("statuses", ["observed"])
            statuses = require_string_list(
                statuses,
                f"fields[{index}].quality_contract.statuses",
                nonempty=True,
            )
            if not set(statuses).issubset(state_quality.USABLE_STATUSES):
                raise ContractError(f"field {field_path} metadata contract permits blocked quality")
            if set(statuses) != set(field["actionable_quality"]):
                raise ContractError(f"field {field_path} actionable quality drifts from metadata statuses")
            for key in ("semantics", "units", "scopes", "completeness"):
                require_string_list(
                    quality_contract.get(key),
                    f"fields[{index}].quality_contract.{key}",
                    nonempty=True,
                )
            if "sources" in quality_contract:
                require_string_list(
                    quality_contract.get("sources"),
                    f"fields[{index}].quality_contract.sources",
                    nonempty=True,
                )
            if "contract_ids" in quality_contract:
                contract_ids = require_string_list(
                    quality_contract.get("contract_ids"),
                    f"fields[{index}].quality_contract.contract_ids",
                    nonempty=True,
                )
                if len(contract_ids) != len(set(contract_ids)):
                    raise ContractError(f"field {field_path} quality contract IDs contain duplicates")
            if field["unit"] not in quality_contract["units"]:
                raise ContractError(f"field {field_path} wire unit is absent from quality metadata units")
            if not isinstance(quality_contract.get("captured_at"), str) or not quality_contract["captured_at"]:
                raise ContractError(f"field {field_path} quality has no capture binding")

    field_paths = {field["path"] for field in fields}
    for field in fields:
        quality_contract = field.get("quality_contract")
        if not isinstance(quality_contract, dict):
            continue
        captured_at = quality_contract["captured_at"]
        require_path(captured_at)
        if captured_at != "snapshot.ts" and captured_at not in field_paths:
            raise ContractError(
                f"field {field['path']} quality capture binding is not a contracted timestamp path: {captured_at}"
            )

    validate_producer_constraints(contract)


def validate_producer_constraints(contract: Dict[str, Any]) -> None:
    constraints = contract.get("producer_constraints")
    if not isinstance(constraints, dict):
        raise ContractError("producer_constraints must be an object")
    source_path = constraints.get("source_path")
    reset_source_path = constraints.get("reset_source_path")
    if not isinstance(source_path, str) or not isinstance(reset_source_path, str):
        raise ContractError("producer source paths must be strings")
    source = (PROJECT_ROOT / source_path).resolve()
    reset_source = (PROJECT_ROOT / reset_source_path).resolve()
    try:
        source_text = source.read_text(encoding="utf-8-sig")
        reset_text = reset_source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"cannot read producer source: {error}") from error

    for label in (
        "cell_budget",
        "time_budget_ms",
        "aggregate_max_age_seconds",
        "observation_scope_identity_limit",
    ):
        item = constraints.get(label)
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not exact_number(item.get("value")):
            raise ContractError(f"producer_constraints.{label} is invalid")
        literal = str(item["value"])
        pattern = rf"\b{re.escape(item['name'])}\s*=\s*{re.escape(literal)}\s*;"
        if re.search(pattern, source_text) is None:
            raise ContractError(f"producer {label} constant does not match {literal}")

    for marker in require_string_list(
        constraints.get("required_source_markers"),
        "producer_constraints.required_source_markers",
        nonempty=True,
    ):
        if marker not in source_text:
            raise ContractError(f"producer source marker is missing: {marker}")
    for pattern in require_string_list(
        constraints.get("required_source_patterns"),
        "producer_constraints.required_source_patterns",
        nonempty=True,
    ):
        try:
            matched = re.search(pattern, source_text) is not None
        except re.error as error:
            raise ContractError(f"producer source pattern is invalid: {pattern}: {error}") from error
        if not matched:
            raise ContractError(f"producer source pattern is missing: {pattern}")
    for marker in require_string_list(
        constraints.get("forbidden_source_markers"),
        "producer_constraints.forbidden_source_markers",
    ):
        if marker in source_text:
            raise ContractError(f"forbidden unbounded producer marker remains: {marker}")
    for marker in require_string_list(
        constraints.get("reset_source_markers"),
        "producer_constraints.reset_source_markers",
        nonempty=True,
    ):
        if marker not in reset_text:
            raise ContractError(f"session reset marker is missing: {marker}")

    food_source_path = constraints.get("food_source_path")
    food_limit = constraints.get("food_enumeration_limit")
    if not isinstance(food_source_path, str):
        raise ContractError("producer_constraints.food_source_path must be a string")
    if (
        not isinstance(food_limit, dict)
        or not isinstance(food_limit.get("name"), str)
        or not exact_number(food_limit.get("value"))
    ):
        raise ContractError("producer_constraints.food_enumeration_limit is invalid")
    try:
        food_source_text = (PROJECT_ROOT / food_source_path).resolve().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"cannot read food producer source: {error}") from error
    food_literal = str(food_limit["value"])
    food_pattern = rf"\b{re.escape(food_limit['name'])}\s*=\s*{re.escape(food_literal)}\s*;"
    if re.search(food_pattern, food_source_text) is None:
        raise ContractError(f"food producer enumeration limit does not match {food_literal}")
    for marker in require_string_list(
        constraints.get("required_food_source_markers"),
        "producer_constraints.required_food_source_markers",
        nonempty=True,
    ):
        if marker not in food_source_text:
            raise ContractError(f"food producer source marker is missing: {marker}")
    for marker in require_string_list(
        constraints.get("forbidden_food_source_markers"),
        "producer_constraints.forbidden_food_source_markers",
        nonempty=True,
    ):
        if marker in food_source_text:
            raise ContractError(f"forbidden food producer marker remains: {marker}")

    duplicant_source_path = constraints.get("duplicant_source_path")
    duplicant_limit = constraints.get("duplicant_count_limit")
    duplicant_priority_group_limit = constraints.get("duplicant_priority_group_limit")
    if not isinstance(duplicant_source_path, str):
        raise ContractError("producer_constraints.duplicant_source_path must be a string")
    if (
        not isinstance(duplicant_limit, dict)
        or not isinstance(duplicant_limit.get("name"), str)
        or not exact_number(duplicant_limit.get("value"))
    ):
        raise ContractError("producer_constraints.duplicant_count_limit is invalid")
    if (
        not isinstance(duplicant_priority_group_limit, dict)
        or not isinstance(duplicant_priority_group_limit.get("name"), str)
        or not exact_number(duplicant_priority_group_limit.get("value"))
    ):
        raise ContractError("producer_constraints.duplicant_priority_group_limit is invalid")
    try:
        duplicant_source_text = (PROJECT_ROOT / duplicant_source_path).resolve().read_text(
            encoding="utf-8-sig"
        )
    except (OSError, UnicodeError) as error:
        raise ContractError(f"cannot read duplicant producer source: {error}") from error
    duplicant_literal = str(duplicant_limit["value"])
    duplicant_limit_pattern = (
        rf"\b{re.escape(duplicant_limit['name'])}\s*=\s*{re.escape(duplicant_literal)}\s*;"
    )
    if re.search(duplicant_limit_pattern, duplicant_source_text) is None:
        raise ContractError(
            f"duplicant producer count limit does not match {duplicant_literal}"
        )
    priority_group_literal = str(duplicant_priority_group_limit["value"])
    priority_group_limit_pattern = (
        rf"\b{re.escape(duplicant_priority_group_limit['name'])}\s*=\s*"
        rf"{re.escape(priority_group_literal)}\s*;"
    )
    if re.search(priority_group_limit_pattern, duplicant_source_text) is None:
        raise ContractError(
            "duplicant priority-group limit does not match " + priority_group_literal
        )
    for marker in require_string_list(
        constraints.get("required_duplicant_source_markers"),
        "producer_constraints.required_duplicant_source_markers",
        nonempty=True,
    ):
        if marker not in duplicant_source_text:
            raise ContractError(f"duplicant producer source marker is missing: {marker}")
    for pattern in require_string_list(
        constraints.get("required_duplicant_source_patterns"),
        "producer_constraints.required_duplicant_source_patterns",
        nonempty=True,
    ):
        try:
            matched = re.search(pattern, duplicant_source_text) is not None
        except re.error as error:
            raise ContractError(f"invalid duplicant producer source regex: {error}") from error
        if not matched:
            raise ContractError(f"duplicant producer source pattern is missing: {pattern}")
    for marker in require_string_list(
        constraints.get("forbidden_duplicant_source_markers", []),
        "producer_constraints.forbidden_duplicant_source_markers",
    ):
        if marker in duplicant_source_text:
            raise ContractError(f"forbidden duplicant producer marker remains: {marker}")

    alert_paths = {
        "catalog": constraints.get("alert_catalog_source_path"),
        "reader": constraints.get("alert_reader_source_path"),
        "integration": constraints.get("alert_integration_source_path"),
        "contract": constraints.get("alert_contract_source_path"),
    }
    if any(not isinstance(item, str) or not item for item in alert_paths.values()):
        raise ContractError("alert producer source paths must be non-empty strings")
    alert_texts: Dict[str, str] = {}
    try:
        for name, relative_path in alert_paths.items():
            alert_texts[name] = (PROJECT_ROOT / relative_path).resolve().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"cannot read canonical alert producer source: {error}") from error
    for name in ("catalog", "reader", "integration", "contract"):
        for marker in require_string_list(
            constraints.get(f"required_alert_{name}_markers"),
            f"producer_constraints.required_alert_{name}_markers",
            nonempty=True,
        ):
            if marker not in alert_texts[name]:
                raise ContractError(f"canonical alert {name} marker is missing: {marker}")
    for name in ("reader", "integration"):
        for marker in require_string_list(
            constraints.get(f"forbidden_alert_{name}_markers", []),
            f"producer_constraints.forbidden_alert_{name}_markers",
        ):
            if marker in alert_texts[name]:
                raise ContractError(f"forbidden canonical alert {name} marker remains: {marker}")

    report_source_path = constraints.get("report_source_path")
    report_integration_source_path = constraints.get("report_integration_source_path")
    if not isinstance(report_source_path, str) or not isinstance(report_integration_source_path, str):
        raise ContractError("report producer source paths must be strings")
    try:
        report_source_text = (PROJECT_ROOT / report_source_path).resolve().read_text(encoding="utf-8-sig")
        report_integration_text = (PROJECT_ROOT / report_integration_source_path).resolve().read_text(
            encoding="utf-8-sig"
        )
    except (OSError, UnicodeError) as error:
        raise ContractError(f"cannot read report producer source: {error}") from error
    for marker in require_string_list(
        constraints.get("required_report_source_markers"),
        "producer_constraints.required_report_source_markers",
        nonempty=True,
    ):
        if marker not in report_source_text:
            raise ContractError(f"report producer source marker is missing: {marker}")
    for pattern in require_string_list(
        constraints.get("required_report_source_patterns"),
        "producer_constraints.required_report_source_patterns",
        nonempty=True,
    ):
        try:
            matched = re.search(pattern, report_source_text) is not None
        except re.error as error:
            raise ContractError(f"report producer source pattern is invalid: {pattern}: {error}") from error
        if not matched:
            raise ContractError(f"report producer source pattern is missing: {pattern}")
    for marker in require_string_list(
        constraints.get("forbidden_report_source_markers"),
        "producer_constraints.forbidden_report_source_markers",
        nonempty=True,
    ):
        if marker in report_source_text:
            raise ContractError(f"forbidden report producer marker remains: {marker}")
    for marker in require_string_list(
        constraints.get("required_report_integration_markers"),
        "producer_constraints.required_report_integration_markers",
        nonempty=True,
    ):
        if marker not in report_integration_text:
            raise ContractError(f"report producer integration marker is missing: {marker}")


def generic_quality_contract_blockers(
    entry: Any,
    contract: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> List[str]:
    if not isinstance(entry, dict):
        return ["quality_entry_missing"]
    blockers: List[str] = []
    statuses = contract.get("statuses", ["observed"])
    if entry.get("status") not in statuses:
        blockers.append("quality_not_observed" if statuses == ["observed"] else "quality_status_mismatch")
    for name, contract_key in (
        ("semantic", "semantics"),
        ("unit", "units"),
        ("scope", "scopes"),
        ("completeness", "completeness"),
    ):
        if entry.get(name) not in contract[contract_key]:
            blockers.append(f"quality_{name}_mismatch")
    source = entry.get("source")
    if not isinstance(source, str) or not source.strip() or source == "none":
        blockers.append("quality_source_missing")
    elif "sources" in contract and source not in contract["sources"]:
        blockers.append("quality_source_mismatch")
    if "contract_ids" in contract and entry.get("contract_id") not in contract["contract_ids"]:
        blockers.append("quality_contract_id_mismatch")
    captured_at = entry.get("captured_at")
    if captured_at != contract["captured_at"]:
        blockers.append("quality_capture_binding_missing")
    else:
        captured_value = snapshot.get("ts") if captured_at == "snapshot.ts" else get_path(snapshot, captured_at)
        try:
            parse_aware_timestamp(captured_value, captured_at)
        except ContractError:
            blockers.append("quality_capture_value_invalid")
    return sorted(set(blockers))


def exact_world_ids(value: Any) -> List[int] | None:
    if not isinstance(value, list):
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value):
        return None
    if len(value) != len(set(value)):
        return None
    return value


def add_cross_field_blockers(snapshot: Mapping[str, Any], blockers: set[str]) -> None:
    """Validate relations that no independent field range can prove safely."""

    sequence = snapshot.get("seq")
    snapshot_sequence = snapshot.get("snapshot_seq")
    if (
        isinstance(sequence, int)
        and not isinstance(sequence, bool)
        and isinstance(snapshot_sequence, int)
        and not isinstance(snapshot_sequence, bool)
        and snapshot_sequence > sequence
    ):
        blockers.add("snapshot_sequence_exceeds_record_sequence")

    cycle = snapshot.get("cycle")
    game_time = snapshot.get("game_time_cycles")
    if exact_number(cycle) and exact_number(game_time) and not (float(cycle) <= float(game_time) < float(cycle) + 1.0):
        blockers.add("game_time_cycle_mismatch")

    scope = snapshot.get("observation_scope")
    if not isinstance(scope, Mapping):
        blockers.add("observation_scope_missing")
    else:
        populated = exact_world_ids(scope.get("populated_world_ids"))
        covered = exact_world_ids(scope.get("covered_world_ids"))
        active_world = scope.get("active_world_id")
        if not populated:
            blockers.add("populated_world_ids_missing")
        if not covered:
            blockers.add("covered_world_ids_missing")
        if populated is not None and covered is not None and not set(populated).issubset(covered):
            blockers.add("populated_world_not_covered")
        if isinstance(active_world, bool) or not isinstance(active_world, int) or active_world < 0:
            blockers.add("active_world_id_invalid")
        else:
            if covered is not None and active_world not in covered:
                blockers.add("active_world_not_covered")
            if covered != [active_world]:
                blockers.add("covered_world_ids_not_active_world_only")
            if scope.get("complete_for_populated_worlds") is True and populated != [active_world]:
                blockers.add("complete_populated_world_ids_not_active_world_only")
        world_id = snapshot.get("world_id")
        if (
            isinstance(active_world, int)
            and not isinstance(active_world, bool)
            and isinstance(world_id, int)
            and not isinstance(world_id, bool)
            and active_world != world_id
        ):
            blockers.add("active_world_identity_mismatch")
        if scope.get("complete_for_populated_worlds") is not True:
            blockers.add("populated_world_coverage_incomplete")

    valid_cells = get_path(snapshot, "materials.active_world_cells.valid_cell_count")
    candidate_cells = get_path(snapshot, "materials.active_world_cells.candidate_cell_count")
    if (
        isinstance(valid_cells, int)
        and not isinstance(valid_cells, bool)
        and isinstance(candidate_cells, int)
        and not isinstance(candidate_cells, bool)
        and valid_cells > candidate_cells
    ):
        blockers.add("material_valid_cells_exceed_candidates")

    scanned_cells = get_path(snapshot, "materials.active_world_cells.scanned_candidate_cell_count")
    remaining_cells = get_path(snapshot, "materials.active_world_cells.remaining_candidate_cell_count")
    scanned_valid_cells = get_path(snapshot, "materials.active_world_cells.scanned_valid_cell_count")
    progress_ratio = get_path(snapshot, "materials.active_world_cells.scan_progress_ratio")
    scan_complete = get_path(snapshot, "materials.active_world_cells.scan_complete")
    scan_status = get_path(snapshot, "materials.active_world_cells.scan_status")
    scan_started_at = get_path(snapshot, "materials.active_world_cells.scan_window_started_at")
    scan_completed_at = get_path(snapshot, "materials.active_world_cells.scan_window_completed_at")
    if all(
        isinstance(item, int) and not isinstance(item, bool) and exact_number(item)
        for item in (candidate_cells, scanned_cells, remaining_cells)
    ):
        if scanned_cells + remaining_cells != candidate_cells:
            blockers.add("material_scan_candidate_accounting_invalid")
        expected_ratio = float(scanned_cells) / float(candidate_cells) if candidate_cells > 0 else 0.0
        if not exact_number(progress_ratio) or abs(float(progress_ratio) - expected_ratio) > 0.000001:
            blockers.add("material_scan_progress_inconsistent")
    if (
        isinstance(scanned_valid_cells, int)
        and not isinstance(scanned_valid_cells, bool)
        and isinstance(scanned_cells, int)
        and not isinstance(scanned_cells, bool)
        and scanned_valid_cells > scanned_cells
    ):
        blockers.add("material_scan_valid_cells_exceed_scanned")
    if scan_complete is True:
        if scanned_cells != candidate_cells or remaining_cells != 0 or scan_status != "complete":
            blockers.add("material_scan_completion_inconsistent")
        if not isinstance(scan_completed_at, str) or not scan_completed_at:
            blockers.add("material_scan_completion_timestamp_missing")
    elif scan_complete is False:
        if scan_status != "partial" or scan_completed_at is not None:
            blockers.add("material_scan_partial_state_inconsistent")

    aggregate_available = get_path(snapshot, "materials.active_world_cells.aggregate_available")
    aggregate_generation = get_path(snapshot, "materials.active_world_cells.aggregate_scan_generation")
    scan_generation = get_path(snapshot, "materials.active_world_cells.scan_generation")
    aggregate_started_at = get_path(snapshot, "materials.active_world_cells.aggregate_window_started_at")
    aggregate_completed_at = get_path(snapshot, "materials.active_world_cells.aggregate_window_completed_at")
    aggregate_age = get_path(snapshot, "materials.active_world_cells.aggregate_age_seconds")
    if aggregate_available is not True:
        blockers.add("material_aggregate_unavailable")
    if (
        isinstance(aggregate_generation, int)
        and not isinstance(aggregate_generation, bool)
        and isinstance(scan_generation, int)
        and not isinstance(scan_generation, bool)
        and aggregate_generation > scan_generation
    ):
        blockers.add("material_aggregate_generation_invalid")
    try:
        snapshot_time = parse_aware_timestamp(snapshot.get("ts"), "ts")
        aggregate_start = parse_aware_timestamp(aggregate_started_at, "aggregate_window_started_at")
        aggregate_end = parse_aware_timestamp(aggregate_completed_at, "aggregate_window_completed_at")
        if aggregate_start > aggregate_end or aggregate_end > snapshot_time:
            blockers.add("material_aggregate_window_invalid")
        expected_age = max(0.0, (snapshot_time - aggregate_end).total_seconds())
        if not exact_number(aggregate_age) or abs(float(aggregate_age) - expected_age) > 0.01:
            blockers.add("material_aggregate_age_inconsistent")
    except ContractError:
        blockers.add("material_aggregate_window_invalid")

    food_quality = get_path(snapshot, "quality.resources.food_kcal")
    if isinstance(food_quality, Mapping):
        food_status = food_quality.get("status")
        food_completeness = food_quality.get("completeness")
        if food_status in ("observed", "estimated") and not (
            (food_status == "observed" and food_completeness == "complete")
            or (food_status == "estimated" and food_completeness == "bounded")
        ):
            blockers.add("food_inventory_quality_consistency_invalid")

    current_report_day = get_path(snapshot, "report_counters.current.report_day")
    previous_report_day = get_path(snapshot, "report_counters.previous_closed.report_day")
    if (
        isinstance(current_report_day, int)
        and not isinstance(current_report_day, bool)
        and isinstance(previous_report_day, int)
        and not isinstance(previous_report_day, bool)
        and previous_report_day >= current_report_day
    ):
        blockers.add("report_counter_epoch_order_invalid")

    stored_joules = get_path(snapshot, "power.stored_joules")
    capacity_joules = get_path(snapshot, "power.capacity_joules")
    battery_percent = get_path(snapshot, "power.battery_percent")
    if exact_number(stored_joules) and exact_number(capacity_joules):
        stored = float(stored_joules)
        capacity = float(capacity_joules)
        if stored > capacity:
            blockers.add("power_stored_exceeds_capacity")
        if capacity > 0.0 and exact_number(battery_percent):
            expected_percent = stored / capacity * 100.0
            if abs(float(battery_percent) - expected_percent) > 0.001:
                blockers.add("power_battery_percent_inconsistent")

    active_generators = get_path(snapshot, "power.active_generator_count")
    generators = get_path(snapshot, "power.generator_count")
    if (
        isinstance(active_generators, int)
        and not isinstance(active_generators, bool)
        and isinstance(generators, int)
        and not isinstance(generators, bool)
        and active_generators > generators
    ):
        blockers.add("power_active_generator_count_invalid")

    rocket_values = [get_path(snapshot, f"rockets.{field}") for field in ("grounded", "launching", "in_flight", "landing")]
    rocket_total = get_path(snapshot, "rockets.total")
    if (
        isinstance(rocket_total, int)
        and not isinstance(rocket_total, bool)
        and all(isinstance(item, int) and not isinstance(item, bool) for item in rocket_values)
        and sum(rocket_values) != rocket_total
    ):
        blockers.add("rocket_count_partition_invalid")


def validate_snapshot(
    snapshot: Dict[str, Any],
    units: Mapping[str, Any],
    contract: Dict[str, Any],
    *,
    now: dt.datetime,
) -> List[str]:
    blockers = set(state_quality.snapshot_trust_blockers(snapshot, now=now))
    for required in contract["required_paths"]:
        if get_path(snapshot, required) is _MISSING:
            blockers.add(f"required_missing:{required}")

    allowed_statuses = set(contract["quality"]["allowed_statuses"])
    for field in contract["fields"]:
        path = field["path"]
        value = get_path(snapshot, path)
        if value is _MISSING:
            blockers.add(f"field_missing:{path}")
        elif not field_type_matches(value, field["type"]):
            blockers.add(f"type_invalid:{path}")
        else:
            if "const" in field and value != field["const"]:
                blockers.add(f"const_mismatch:{path}")
            if "allowed_values" in field and value not in field["allowed_values"]:
                blockers.add(f"value_invalid:{path}")
            if field["type"] == "integer":
                # JSON integers are arbitrary precision in Python. Compare
                # them exactly so an oversized value is range-blocked instead
                # of being skipped merely because binary64 conversion fails.
                if "minimum" in field and value < field["minimum"]:
                    blockers.add(f"range_invalid:{path}")
                if "maximum" in field and value > field["maximum"]:
                    blockers.add(f"range_invalid:{path}")
            elif exact_number(value):
                if "minimum" in field and value < field["minimum"]:
                    blockers.add(f"range_invalid:{path}")
                if "maximum" in field and value > field["maximum"]:
                    blockers.add(f"range_invalid:{path}")
            if isinstance(value, list):
                maximum_items = field.get("maximum_items")
                if isinstance(maximum_items, int) and len(value) > maximum_items:
                    blockers.add(f"list_too_long:{path}")
                if field.get("unique_items") is True and len(value) != len(set(value)):
                    blockers.add(f"list_duplicates:{path}")
                if field.get("sorted_items") is True and value != sorted(value):
                    blockers.add(f"list_unsorted:{path}")
                item_pattern = field.get("item_pattern")
                if isinstance(item_pattern, str) and any(
                    not isinstance(item, str) or re.fullmatch(item_pattern, item) is None for item in value
                ):
                    blockers.add(f"item_invalid:{path}")
                allowed_items = field.get("allowed_items")
                if isinstance(allowed_items, list) and any(item not in allowed_items for item in value):
                    blockers.add(f"item_not_allowed:{path}")

        if get_path(units, path) != field["unit"]:
            blockers.add(f"unit_invalid:{path}")

        if field.get("quality_required") is not True:
            continue
        entry = get_path(snapshot, f"quality.{path}")
        quality_contract = field.get("quality_contract")
        if isinstance(quality_contract, dict):
            statuses = quality_contract.get("statuses", ["observed"])
            if (
                statuses == ["observed"]
                and quality_contract["captured_at"] == "snapshot.ts"
                and "sources" not in quality_contract
                and "contract_ids" not in quality_contract
            ):
                metadata_blockers = state_quality.quality_contract_blockers(
                    entry,
                    semantics=tuple(quality_contract["semantics"]),
                    units=tuple(quality_contract["units"]),
                    scopes=tuple(quality_contract["scopes"]),
                    completeness=tuple(quality_contract["completeness"]),
                )
            else:
                metadata_blockers = generic_quality_contract_blockers(entry, quality_contract, snapshot)
            blockers.update(f"{code}:{path}" for code in metadata_blockers)
            continue
        if not isinstance(entry, dict):
            blockers.add(f"quality_missing:{path}")
            continue
        status = entry.get("status")
        if status not in allowed_statuses:
            blockers.add(f"quality_status_invalid:{path}")
        elif status not in field["actionable_quality"]:
            blockers.add(f"quality_unusable:{path}")
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            blockers.add(f"quality_source_invalid:{path}")

    add_cross_field_blockers(snapshot, blockers)
    return sorted(blockers)


def expected_binding(step_id: str, blockers: Sequence[str]) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
    accepted = not blockers
    command_eligible = accepted
    event_type = ACCEPT_EVENT if accepted else REJECT_EVENT
    event_data = {
        "step_id": step_id,
        "accepted": accepted,
        "command_eligible": command_eligible,
        "blockers": list(blockers),
    }
    prefix = f"oni.telemetry.contract.{step_id}"
    state = {
        f"{prefix}.accepted": accepted,
        f"{prefix}.command_eligible": command_eligible,
        f"{prefix}.blocker_count": len(blockers),
        f"{prefix}.blockers": list(blockers),
    }
    return event_type, event_data, state


def validate_step_binding(
    step: Dict[str, Any],
    base_snapshot: Dict[str, Any],
    base_units: Dict[str, Any],
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    step_id = step.get("id")
    if not isinstance(step_id, str) or not step_id:
        raise ContractError("each step must have a non-empty id")
    action = step.get("action")
    expect = step.get("expect")
    evidence = step.get("evidence")
    if not isinstance(action, dict) or action.get("type") != "mock":
        raise ContractError(f"{step_id}: action must be a game-mod-verify mock action")
    if not isinstance(expect, dict):
        raise ContractError(f"{step_id}: expect must be an object")
    if not isinstance(evidence, dict) or evidence.get("events") is not True or evidence.get("state") is not True:
        raise ContractError(f"{step_id}: evidence must capture events and state")

    check = action.get("telemetry_contract_check")
    if not isinstance(check, dict):
        raise ContractError(f"{step_id}: action has no telemetry_contract_check")
    snapshot = copy.deepcopy(base_snapshot)
    units = copy.deepcopy(base_units)
    apply_mutations(snapshot, check.get("mutations"))
    apply_mutations(units, check.get("unit_mutations"))
    now = parse_aware_timestamp(check.get("now"), f"{step_id}.now")
    blockers = validate_snapshot(snapshot, units, contract, now=now)
    accepted = not blockers

    declared = check.get("expected")
    computed = {
        "accepted": accepted,
        "command_eligible": accepted,
        "blockers": blockers,
    }
    if declared != computed:
        raise ContractError(
            f"{step_id}: declared contract outcome does not match computed outcome; "
            f"declared={declared!r} computed={computed!r}"
        )

    event_type, event_data, state_binding = expected_binding(step_id, blockers)
    opposite_event = REJECT_EVENT if event_type == ACCEPT_EVENT else ACCEPT_EVENT
    events = action.get("events")
    if not isinstance(events, list):
        raise ContractError(f"{step_id}: action.events must be an array")
    matching = [
        item
        for item in events
        if isinstance(item, dict) and item.get("type") == event_type and item.get("data") == event_data
    ]
    if len(matching) != 1:
        raise ContractError(f"{step_id}: action must emit exactly one correctly bound {event_type} event")
    if any(isinstance(item, dict) and item.get("type") == opposite_event for item in events):
        raise ContractError(f"{step_id}: action emits contradictory {opposite_event} event")
    action_state = action.get("state")
    if not isinstance(action_state, dict) or any(action_state.get(key, _MISSING) != value for key, value in state_binding.items()):
        raise ContractError(f"{step_id}: action.state is not bound to computed fail-closed outcome")

    expected_events = expect.get("events")
    forbidden_events = expect.get("forbid_events")
    expected_state = expect.get("state")
    if not isinstance(expected_events, list) or event_type not in expected_events:
        raise ContractError(f"{step_id}: expect.events does not require {event_type}")
    if not isinstance(forbidden_events, list) or opposite_event not in forbidden_events:
        raise ContractError(f"{step_id}: expect.forbid_events does not forbid {opposite_event}")
    if not isinstance(expected_state, dict) or any(expected_state.get(key, _MISSING) != value for key, value in state_binding.items()):
        raise ContractError(f"{step_id}: expect.state is not bound to computed fail-closed outcome")
    timeout_ms = expect.get("timeout_ms")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= 1000:
        raise ContractError(f"{step_id}: timeout_ms must be an integer from 0 through 1000")

    return {
        "id": step_id,
        "accepted": accepted,
        "command_eligible": accepted,
        "blockers": blockers,
        "event": event_type,
    }


def validate_scenario(path: pathlib.Path) -> Dict[str, Any]:
    scenario = load_json(path)
    scenario_id = scenario.get("id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ContractError(f"{path} has no non-empty id")
    if scenario.get("profile") != "mock-oni":
        raise ContractError(f"{scenario_id}: profile must be mock-oni")
    metadata = scenario.get("telemetry_contract")
    if not isinstance(metadata, dict):
        raise ContractError(f"{scenario_id}: telemetry_contract metadata is missing")
    relative_contract = metadata.get("path")
    if not isinstance(relative_contract, str) or not relative_contract:
        raise ContractError(f"{scenario_id}: telemetry_contract.path is missing")
    contract_path = (path.parent / relative_contract).resolve()
    contract = load_json(contract_path)
    validate_contract_definition(contract, contract_path)
    base_snapshot = metadata.get("base_snapshot")
    base_units = metadata.get("base_units")
    if not isinstance(base_snapshot, dict) or not isinstance(base_units, dict):
        raise ContractError(f"{scenario_id}: base_snapshot and base_units must be objects")

    steps = scenario.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ContractError(f"{scenario_id}: steps must be a non-empty array")
    results = [validate_step_binding(step, base_snapshot, base_units, contract) for step in steps]
    ids = [item["id"] for item in results]
    if len(ids) != len(set(ids)):
        raise ContractError(f"{scenario_id}: step ids must be unique")
    if not any(item["accepted"] for item in results):
        raise ContractError(f"{scenario_id}: no positive contract example is bound")
    if not any(not item["accepted"] for item in results):
        raise ContractError(f"{scenario_id}: no fail-closed contract example is bound")
    return {
        "id": scenario_id,
        "path": str(path),
        "contract": str(contract_path),
        "status": "pass",
        "steps": results,
    }


def discover_scenarios(arguments: Iterable[str]) -> List[pathlib.Path]:
    supplied = [pathlib.Path(item).resolve() for item in arguments]
    return supplied or sorted(DEFAULT_SCENARIO_DIR.glob("*.json"))


def contract_units(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the wire-unit registry declared by the immutable contract.

    Live ``oni.state.v2`` snapshots intentionally do not duplicate a top-level
    unit tree.  Unit metadata for actionable values remains in ``quality``;
    the remaining structural fields are interpreted using this contract-owned
    registry.  This function never changes or fills the input snapshot.
    """

    units: Dict[str, Any] = {}
    for field in contract["fields"]:
        set_path(units, field["path"], field["unit"])
    return units


def health_binding_blockers(
    health: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    state_hash: str,
) -> List[str]:
    """Fail closed unless Bridge health is trusted and bound to this state."""

    blockers: set[str] = set()
    if health.get("schema") != "oni.bridge.health.v1":
        blockers.add("health_schema_invalid")
    if health.get("status") != "ok":
        blockers.add("health_status_not_ok")
    if health.get("trust_ready") is not True:
        blockers.add("health_trust_not_ready")

    trust_blockers = health.get("trust_blockers")
    if not isinstance(trust_blockers, list):
        blockers.add("health_trust_blockers_invalid")
    elif trust_blockers:
        blockers.add("health_trust_blockers_nonempty")
    declared_blockers = health.get("blockers", [])
    if not isinstance(declared_blockers, list):
        blockers.add("health_blockers_invalid")
    elif declared_blockers:
        blockers.add("health_blockers_nonempty")

    source = health.get("source")
    if not isinstance(source, Mapping):
        blockers.add("health_source_identity_missing")
    else:
        expected = {
            "session_id": snapshot.get("session_id"),
            "snapshot_id": snapshot.get("snapshot_id"),
            "state_hash": state_hash,
        }
        for field, value in expected.items():
            if source.get(field) != value:
                blockers.add(f"health_source_{field}_mismatch")

    top_level_bindings = {
        "current_session_id": snapshot.get("session_id"),
        "latest_snapshot_id": snapshot.get("snapshot_id"),
        "latest_seq": snapshot.get("seq"),
    }
    for field, value in top_level_bindings.items():
        if health.get(field) != value:
            blockers.add(f"health_{field}_mismatch")
    return sorted(blockers)


def live_time_binding(
    snapshot: Mapping[str, Any],
    now: dt.datetime,
    health: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    age, stale_after, freshness_blockers = state_quality.snapshot_freshness(dict(snapshot), now=now)
    health_updated_at = health.get("updated_at") if isinstance(health, Mapping) else None
    health_age: float | None = None
    if health_updated_at is not None:
        try:
            health_time = parse_aware_timestamp(health_updated_at, "health.updated_at")
            health_age = max(0.0, (now - health_time).total_seconds())
        except ContractError:
            health_age = None
    return {
        "snapshot_ts": snapshot.get("ts"),
        "validated_at": now.isoformat(),
        "snapshot_age_seconds": None if age is None else round(age, 6),
        "stale_after_seconds": None if stale_after is None else round(stale_after, 6),
        "maximum_future_skew_seconds": state_quality.MAX_FUTURE_CLOCK_SKEW_SECONDS,
        "freshness_blockers": freshness_blockers,
        "health_updated_at": health_updated_at,
        "health_age_seconds": None if health_age is None else round(health_age, 6),
    }


def validate_live_snapshot(
    snapshot: Dict[str, Any],
    contract: Dict[str, Any],
    *,
    now: dt.datetime,
    health: Dict[str, Any] | None = None,
    snapshot_path: pathlib.Path | None = None,
    health_path: pathlib.Path | None = None,
    contract_path: pathlib.Path | None = None,
) -> Dict[str, Any]:
    """Validate one unmodified Probe/Bridge snapshot against the live contract."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ContractError("live validation --now must include a UTC offset")
    state_hash = state_quality.canonical_state_hash(snapshot)
    snapshot_blockers = validate_snapshot(snapshot, contract_units(contract), contract, now=now)
    health_blockers = (
        health_binding_blockers(health, snapshot, state_hash=state_hash)
        if isinstance(health, dict)
        else []
    )
    accepted = not snapshot_blockers
    command_blockers = list(snapshot_blockers) + list(health_blockers)
    if health is None:
        command_blockers.append("health_not_provided")
    command_blockers = sorted(set(command_blockers))
    command_eligible = accepted and health is not None and not command_blockers
    overall_pass = accepted and (health is None or command_eligible)

    source_identity = {
        "session_id": snapshot.get("session_id"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "save_id_hash": snapshot.get("save_id_hash"),
        "world_id": snapshot.get("world_id"),
        "seq": snapshot.get("seq"),
        "snapshot_seq": snapshot.get("snapshot_seq"),
        "state_hash": state_hash,
    }
    return {
        "schema": "oni.telemetry-live-verification.v1",
        "mode": "probe_snapshot",
        "status": "pass" if overall_pass else "fail",
        "accepted": accepted,
        "command_eligible": command_eligible,
        "blockers": snapshot_blockers,
        "snapshot_blockers": snapshot_blockers,
        "health_blockers": health_blockers,
        "command_blockers": command_blockers,
        "source": source_identity,
        "time_binding": live_time_binding(snapshot, now, health),
        "health_binding": {
            "provided": health is not None,
            "accepted": health is not None and not health_blockers,
            "source_identity_matched": health is not None
            and not any(
                blocker == "health_source_identity_missing"
                or blocker.endswith("_mismatch")
                for blocker in health_blockers
            ),
            "source": copy.deepcopy(health.get("source")) if isinstance(health, dict) else None,
        },
        "inputs": {
            "snapshot": None if snapshot_path is None else str(snapshot_path),
            "health": None if health_path is None else str(health_path),
            "contract": None if contract_path is None else str(contract_path),
        },
    }


def write_json_atomic(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=str(path.parent),
            suffix=".tmp",
        ) as handle:
            temporary = pathlib.Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def parse_live_now(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now().astimezone()
    return parse_aware_timestamp(value, "--now")


def validate_live_paths(
    args: argparse.Namespace,
) -> tuple[pathlib.Path, pathlib.Path | None, pathlib.Path, pathlib.Path | None]:
    snapshot_path = pathlib.Path(args.snapshot).resolve()
    health_path = pathlib.Path(args.health).resolve() if args.health else None
    contract_path = pathlib.Path(args.contract).resolve()
    output_path = pathlib.Path(args.output).resolve() if args.output else None
    if output_path is not None:
        protected = {snapshot_path, contract_path}
        if health_path is not None:
            protected.add(health_path)
        if output_path in protected:
            raise ContractError("--output must not overwrite a snapshot, health, or contract input")
    return snapshot_path, health_path, contract_path, output_path


def run_live_validation(args: argparse.Namespace) -> tuple[int, Dict[str, Any]]:
    snapshot_path, health_path, contract_path, output_path = validate_live_paths(args)
    snapshot = load_json(snapshot_path)
    health = load_json(health_path) if health_path is not None else None
    contract = load_json(contract_path)
    validate_contract_definition(contract, contract_path)
    report = validate_live_snapshot(
        snapshot,
        contract,
        now=parse_live_now(args.now),
        health=health,
        snapshot_path=snapshot_path,
        health_path=health_path,
        contract_path=contract_path,
    )
    if output_path is not None:
        write_json_atomic(output_path, report)
    return (0 if report["status"] == "pass" else 1), report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scenario", action="append", default=[], help="Scenario JSON path; repeatable")
    mode.add_argument("--snapshot", help="Live Probe/Bridge latest_state.json to validate without mutation")
    parser.add_argument("--health", help="Optional Bridge health.json to bind to the live snapshot")
    parser.add_argument(
        "--contract",
        default=str(DEFAULT_CONTRACT_PATH),
        help="Telemetry contract JSON for live mode",
    )
    parser.add_argument("--now", help="Aware RFC3339 validation time for live mode; defaults to local aware now")
    parser.add_argument("--output", help="Write the live validation report atomically to this JSON path")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.snapshot:
        try:
            code, payload = run_live_validation(args)
        except (ContractError, OSError, TypeError, KeyError, ValueError, OverflowError) as error:
            payload = {
                "schema": "oni.telemetry-live-verification.v1",
                "mode": "probe_snapshot",
                "status": "error",
                "accepted": False,
                "command_eligible": False,
                "blockers": ["validator_error"],
                "snapshot_blockers": ["validator_error"],
                "health_blockers": [],
                "command_blockers": ["validator_error"],
                "error": str(error),
            }
            code = 2
            if args.output:
                try:
                    output_path = pathlib.Path(args.output).resolve()
                    protected = {
                        pathlib.Path(args.snapshot).resolve(),
                        pathlib.Path(args.contract).resolve(),
                    }
                    if args.health:
                        protected.add(pathlib.Path(args.health).resolve())
                    if output_path not in protected:
                        write_json_atomic(output_path, payload)
                except OSError:
                    pass
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif code == 0:
            print(
                f"PASS {payload['source']['snapshot_id']}: accepted; "
                f"command_eligible={str(payload['command_eligible']).lower()}; blockers=none"
            )
        else:
            blockers = ", ".join(payload.get("command_blockers") or payload.get("blockers") or []) or "none"
            print(
                f"FAIL {payload.get('source', {}).get('snapshot_id') or args.snapshot}: "
                f"accepted={str(bool(payload.get('accepted'))).lower()}; "
                f"command_eligible={str(bool(payload.get('command_eligible'))).lower()}; "
                f"blockers={blockers}",
                file=sys.stderr,
            )
            if payload.get("error"):
                print(f"  {payload['error']}", file=sys.stderr)
        return code

    if (
        args.health
        or args.now
        or args.output
        or pathlib.Path(args.contract).resolve() != DEFAULT_CONTRACT_PATH.resolve()
    ):
        print("--health, --contract, --now, and --output require --snapshot", file=sys.stderr)
        return 2
    paths = discover_scenarios(args.scenario)
    if not paths:
        print(f"No telemetry scenarios found under {DEFAULT_SCENARIO_DIR}", file=sys.stderr)
        return 2

    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    for path in paths:
        try:
            results.append(validate_scenario(path))
        except (ContractError, TypeError, KeyError, ValueError, OverflowError) as error:
            failures.append({"path": str(path), "error": str(error)})

    payload = {
        "schema": "oni.telemetry-contract-verification.v1",
        "status": "pass" if not failures else "fail",
        "scenario_count": len(paths),
        "results": results,
        "failures": failures,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"PASS {result['id']} ({len(result['steps'])} bound checks)")
            for step in result["steps"]:
                disposition = "accepted" if step["accepted"] else "rejected"
                blocker_text = ", ".join(step["blockers"]) or "none"
                print(f"  PASS {step['id']}: {disposition}; blockers={blocker_text}")
        for failure in failures:
            print(f"FAIL {failure['path']}: {failure['error']}", file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
