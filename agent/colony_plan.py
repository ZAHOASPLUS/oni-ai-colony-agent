from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from agent.advanced_actions import load_catalog
from agent.canonical_hash import canonical_hash as canonical_document_hash
from agent.action_outcomes import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_REJECTED,
    confirmed_command_commit_blockers,
    read_action_outcome,
)
from agent.command_custody import (
    CommandCustodyError,
    CommandCustodyUnknown,
    lifecycle_custody,
    observe_pending_envelope,
    publish_pending_command,
    transition_pending_envelope,
)
from agent.command_queue import (
    COMMAND_SCHEMA,
    command_pipeline_readiness,
    ensure_command_dirs,
    load_policy,
    write_json_atomic,
)
from agent.decision import DecisionFrameError, validate_decision_frame
from agent.decision_frame_archive import (
    CurrentDecisionContext,
    DecisionFrameArchiveError,
    assert_current_decision_context,
    load_current_decision_context,
)
from agent.field_contracts import (
    condition_contract_blockers,
    condition_declaration_blockers,
)
from agent.state_quality import (
    canonical_state_hash,
    field_quality,
    safe_get,
    snapshot_age_seconds,
    stale_after_seconds,
)


PLAN_SCHEMA = "oni.colony-plan.v1"
PLAN_DEFINITION_SCHEMA = "oni.colony-plan-definition.v1"
PLAN_POINTER_SCHEMA = "oni.colony-plan-pointer.v1"
PLAN_COMMAND_SOURCE = "colony_plan"
PLAN_STATUSES = {"draft", "active", "paused", "completed", "aborted", "failed"}
TERMINAL_PLAN_STATUSES = {"completed", "aborted", "failed"}
STEP_STATUSES = {
    "waiting",
    "awaiting_approval",
    "awaiting_confirmation",
    "completed",
    "blocked",
    "failed",
    "cancelled",
}
CONDITION_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains"}
MAX_PLAN_STEPS = 32
MAX_PLAN_HISTORY = 100
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_PLAN_ID = re.compile(r"^plan-[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
ABORT_BLOCKING_STAGES = frozenset({"approving", "approved", "executing", "confirming"})


class ColonyPlanError(RuntimeError):
    pass


class PlanRecoveryRequired(ColonyPlanError):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def plans_root(bridge_dir: pathlib.Path) -> pathlib.Path:
    return bridge_dir / "colony-plans"


def plan_directory(bridge_dir: pathlib.Path, plan_id: str) -> pathlib.Path:
    if not SAFE_PLAN_ID.fullmatch(plan_id):
        raise ColonyPlanError("Invalid colony plan id.")
    return plans_root(bridge_dir) / plan_id


def _load_json(path: pathlib.Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ColonyPlanError(f"Required file is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ColonyPlanError(f"Required JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ColonyPlanError(f"Required JSON must be an object: {path}")
    return value


def _assert_legacy_plan_lock_absent(bridge_dir: pathlib.Path) -> None:
    """Reject the retired plan lock without following, removing, or aging it."""

    lock_path = plans_root(bridge_dir) / ".mutation.lock"
    try:
        os.lstat(lock_path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ColonyPlanError(
            "The retired colony-plan mutation lock cannot be inspected; manual review is required."
        ) from exc
    raise ColonyPlanError(
        "The retired colony-plan mutation lock remains; remove it only after manual review."
    )


@contextlib.contextmanager
def _plan_lock(bridge_dir: pathlib.Path) -> Iterator[None]:
    """Serialize every plan lifecycle window under the shared OS custody lock."""

    bridge_dir = bridge_dir.expanduser().resolve()
    _assert_legacy_plan_lock_absent(bridge_dir)
    try:
        with lifecycle_custody(bridge_dir):
            _assert_legacy_plan_lock_absent(bridge_dir)
            yield
    except CommandCustodyError as exc:
        raise ColonyPlanError(
            "Colony-plan lifecycle custody failed; the operation was not authorized to continue."
        ) from exc


def _canonical_hash(payload: Dict[str, Any]) -> str:
    value = copy.deepcopy(payload)
    value.pop("revision_hash", None)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _revision_valid(plan: Dict[str, Any]) -> bool:
    return (
        plan.get("schema") == PLAN_SCHEMA
        and isinstance(plan.get("revision"), int)
        and plan.get("revision", 0) > 0
        and isinstance(plan.get("revision_hash"), str)
        and plan.get("revision_hash") == _canonical_hash(plan)
        and plan.get("status") in PLAN_STATUSES
    )


def _append_history(plan: Dict[str, Any], action: str, details: Optional[Dict[str, Any]] = None) -> None:
    history = plan.get("history") if isinstance(plan.get("history"), list) else []
    history.append(
        {
            "at": now_iso(),
            "action": action,
            "details": copy.deepcopy(details or {}),
        }
    )
    plan["history"] = history[-MAX_PLAN_HISTORY:]


def _commit_revision(bridge_dir: pathlib.Path, plan: Dict[str, Any], action: str) -> Dict[str, Any]:
    value = copy.deepcopy(plan)
    previous_hash = value.get("revision_hash")
    value["revision"] = int(value.get("revision") or 0) + 1
    value["previous_revision_hash"] = previous_hash if value["revision"] > 1 else None
    value["updated_at"] = now_iso()
    value["revision_action"] = action
    value.pop("revision_hash", None)
    value["revision_hash"] = _canonical_hash(value)

    directory = plan_directory(bridge_dir, str(value["plan_id"]))
    revision_path = directory / "revisions" / f"{value['revision']:06d}.json"
    if revision_path.exists():
        existing = _load_json(revision_path)
        if existing.get("revision_hash") != value["revision_hash"]:
            raise PlanRecoveryRequired(f"Revision {value['revision']} already exists with different content.")
    else:
        write_json_atomic(revision_path, value)
    write_json_atomic(directory / "current.json", value)
    pointer = {
        "schema": PLAN_POINTER_SCHEMA,
        "plan_id": value["plan_id"],
        "revision": value["revision"],
        "revision_hash": value["revision_hash"],
        "status": value["status"],
        "updated_at": value["updated_at"],
    }
    write_json_atomic(plans_root(bridge_dir) / "active.json", pointer)
    write_json_atomic(bridge_dir / "colony-plan.json", value)
    return value


def _active_plan_id(bridge_dir: pathlib.Path) -> str:
    pointer = _load_json(plans_root(bridge_dir) / "active.json")
    if pointer.get("schema") != PLAN_POINTER_SCHEMA or not SAFE_PLAN_ID.fullmatch(str(pointer.get("plan_id") or "")):
        raise PlanRecoveryRequired("The active colony plan pointer is invalid.")
    return str(pointer["plan_id"])


def load_active_plan(bridge_dir: pathlib.Path, plan_id: Optional[str] = None) -> Dict[str, Any]:
    active_id = _active_plan_id(bridge_dir)
    if plan_id is not None and plan_id != active_id:
        raise ColonyPlanError("The requested plan is not the active colony plan.")
    current = _load_json(plan_directory(bridge_dir, active_id) / "current.json")
    pointer = _load_json(plans_root(bridge_dir) / "active.json")
    if not _revision_valid(current):
        raise PlanRecoveryRequired("The current colony plan revision is invalid.")
    if (
        current.get("plan_id") != active_id
        or pointer.get("revision") != current.get("revision")
        or pointer.get("revision_hash") != current.get("revision_hash")
    ):
        raise PlanRecoveryRequired("The colony plan pointer and current revision disagree.")
    revision_path = plan_directory(bridge_dir, active_id) / "revisions" / f"{current['revision']:06d}.json"
    revision = _load_json(revision_path)
    if revision.get("revision_hash") != current.get("revision_hash") or not _revision_valid(revision):
        raise PlanRecoveryRequired("The current colony plan does not match its immutable revision.")
    return current


def _catalog_leaf_actions() -> Dict[str, Dict[str, Any]]:
    catalog = load_catalog()
    result: Dict[str, Dict[str, Any]] = {}
    for item in catalog.get("actions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("plane") == "game" and item.get("batch") in {"A", "B", "C"}:
            result[str(item.get("id"))] = item
    return result


def _validate_condition(value: Any, label: str, *, action: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ColonyPlanError(f"{label} must be an object.")
    path = value.get("path")
    operator = value.get("op")
    if not isinstance(path, str) or not path or len(path) > 512:
        raise ColonyPlanError(f"{label}.path is invalid.")
    if operator not in CONDITION_OPERATORS:
        raise ColonyPlanError(f"{label}.op is invalid.")
    blockers = condition_declaration_blockers(value, action=action)
    if blockers:
        raise ColonyPlanError(f"{label} violates its field contract: {', '.join(blockers)}.")
    return copy.deepcopy(value)


def _validate_definition(definition: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    if definition.get("schema") != PLAN_DEFINITION_SCHEMA:
        raise ColonyPlanError(f"Plan definition schema must be {PLAN_DEFINITION_SCHEMA}.")
    title = definition.get("title")
    goal = definition.get("goal")
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ColonyPlanError("Plan title must contain 1 through 160 characters.")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 1000:
        raise ColonyPlanError("Plan goal must contain 1 through 1000 characters.")
    raw_steps = definition.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_PLAN_STEPS:
        raise ColonyPlanError(f"Plan steps must contain 1 through {MAX_PLAN_STEPS} entries.")

    leaf_actions = _catalog_leaf_actions()
    steps: List[Dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ColonyPlanError(f"steps[{index}] must be an object.")
        step_id = raw.get("id")
        action = raw.get("action")
        if not isinstance(step_id, str) or not SAFE_ID.fullmatch(step_id):
            raise ColonyPlanError(f"steps[{index}].id is invalid.")
        if step_id in identifiers:
            raise ColonyPlanError(f"Duplicate step id: {step_id}")
        identifiers.add(step_id)
        if action not in leaf_actions:
            raise ColonyPlanError(f"Step {step_id} uses a non-catalogued game leaf action: {action}")
        args = raw.get("args")
        if not isinstance(args, dict):
            raise ColonyPlanError(f"Step {step_id} args must be an object.")
        try:
            encoded_args = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ColonyPlanError(f"Step {step_id} args are not JSON serializable.") from exc
        if len(encoded_args.encode("utf-8")) > 64 * 1024:
            raise ColonyPlanError(f"Step {step_id} args exceed 64 KiB.")
        preconditions = raw.get("preconditions")
        effects = raw.get("expected_effects")
        if not isinstance(preconditions, list) or not preconditions:
            raise ColonyPlanError(f"Step {step_id} must include preconditions.")
        if not isinstance(effects, list) or not effects:
            raise ColonyPlanError(f"Step {step_id} must include expected_effects.")
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise ColonyPlanError(f"Step {step_id} dependencies must be a string list.")
        if len(set(dependencies)) != len(dependencies) or step_id in dependencies:
            raise ColonyPlanError(f"Step {step_id} dependencies are invalid.")
        not_before = raw.get("not_before_cycle")
        not_after = raw.get("not_after_cycle")
        for label, value in (("not_before_cycle", not_before), ("not_after_cycle", not_after)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ColonyPlanError(f"Step {step_id} {label} must be a non-negative integer.")
        if not_before is not None and not_after is not None and not_before > not_after:
            raise ColonyPlanError(f"Step {step_id} has an inverted cycle window.")
        catalog_item = leaf_actions[str(action)]
        steps.append(
            {
                "id": step_id,
                "action": action,
                "args": copy.deepcopy(args),
                "dependencies": list(dependencies),
                "preconditions": [
                    _validate_condition(item, f"Step {step_id} precondition", action=str(action))
                    for item in preconditions
                ],
                "expected_effects": [
                    _validate_condition(item, f"Step {step_id} expected effect", action=str(action))
                    for item in effects
                ],
                "not_before_cycle": not_before,
                "not_after_cycle": not_after,
                "risk": catalog_item.get("risk"),
                "reason": raw.get("reason"),
                "status": "waiting",
                "command_id": None,
                "completed_at": None,
                "result_state_hash": None,
            }
        )

    step_ids = {step["id"] for step in steps}
    for step in steps:
        missing = sorted(set(step["dependencies"]) - step_ids)
        if missing:
            raise ColonyPlanError(f"Step {step['id']} has missing dependencies: {', '.join(missing)}")
    _require_acyclic(steps)
    return title.strip(), goal.strip(), steps


def _validated_decision_provenance(definition: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Accept only bounded, credential-free planner provenance."""
    raw = definition.get("decision_provenance")
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("schema") != "oni.llm-decision-provenance.v1":
        raise ColonyPlanError("decision_provenance schema is invalid.")
    result: Dict[str, Any] = {"schema": raw["schema"]}
    for key in ("mode", "prompt_hash", "incident_fingerprint", "response_hash", "model", "status"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 512:
            raise ColonyPlanError(f"decision_provenance.{key} is invalid.")
        result[key] = value
    if result.get("mode") not in {"rule_only", "llm_ranked"}:
        raise ColonyPlanError("decision_provenance.mode is invalid.")
    for key in ("prompt_hash", "incident_fingerprint", "response_hash"):
        value = result.get(key)
        if value is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ColonyPlanError(f"decision_provenance.{key} must be a sha256 digest.")
    source = raw.get("source")
    if source is not None:
        if not isinstance(source, dict):
            raise ColonyPlanError("decision_provenance.source is invalid.")
        checked_source: Dict[str, Any] = {}
        for key in ("session_id", "save_id_hash", "world_id", "snapshot_id", "state_hash"):
            value = source.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int)) or (isinstance(value, str) and len(value) > 512):
                raise ColonyPlanError(f"decision_provenance.source.{key} is invalid.")
            checked_source[key] = value
        if "state_hash" in checked_source and re.fullmatch(r"sha256:[0-9a-f]{64}", checked_source["state_hash"]) is None:
            raise ColonyPlanError("decision_provenance.source.state_hash must be a sha256 digest.")
        result["source"] = checked_source
    return result


def _require_acyclic(steps: List[Dict[str, Any]]) -> None:
    dependencies = {str(step["id"]): list(step["dependencies"]) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            raise ColonyPlanError("Plan step dependencies contain a cycle.")
        visiting.add(step_id)
        for dependency in dependencies[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for identifier in dependencies:
        visit(identifier)


def _load_live_state(bridge_dir: pathlib.Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    frame_before = _load_json(bridge_dir / "decision-frame.json")
    try:
        validate_decision_frame(frame_before)
    except DecisionFrameError as exc:
        raise ColonyPlanError("The decision-frame commit marker is invalid.") from exc
    state = _load_json(bridge_dir / "latest_state.json")
    health = _load_json(bridge_dir / "health.json")
    frame_after = _load_json(bridge_dir / "decision-frame.json")
    if frame_before.get("frame_id") != frame_after.get("frame_id"):
        raise ColonyPlanError("The live output generation changed while the plan bundle was read.")
    if state.get("schema") != "oni.state.v2" or state.get("event") != "state_snapshot":
        raise ColonyPlanError("A current oni.state.v2 snapshot is required.")
    source = frame_before.get("source") if isinstance(frame_before.get("source"), dict) else {}
    expected = {
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        "snapshot_id": state.get("snapshot_id"),
        "seq": state.get("seq"),
        "snapshot_seq": state.get("snapshot_seq"),
        "ts": state.get("ts"),
        "state_hash": canonical_state_hash(state),
    }
    if not _strict_json_equal(source, expected):
        raise ColonyPlanError("The latest state does not match the committed decision frame source.")
    health_source = health.get("source") if isinstance(health.get("source"), dict) else {}
    if any(health_source.get(field) != expected.get(field) for field in ("session_id", "snapshot_id", "state_hash")):
        raise ColonyPlanError("The health output does not match the committed state generation.")
    return state, health


def _authorizing_decision_context(
    bridge_dir: pathlib.Path,
    state: Dict[str, Any],
) -> CurrentDecisionContext:
    try:
        context = load_current_decision_context(bridge_dir, mode="planned")
    except DecisionFrameArchiveError as exc:
        raise ColonyPlanError("The decision frame cannot authorize a plan leaf.") from exc
    source = context.frame.get("source") if isinstance(context.frame.get("source"), dict) else {}
    if (
        source.get("session_id") != state.get("session_id")
        or source.get("save_id_hash") != state.get("save_id_hash")
        or source.get("world_id") != state.get("world_id")
        or source.get("snapshot_id") != state.get("snapshot_id")
        or source.get("snapshot_seq") != state.get("snapshot_seq")
        or source.get("state_hash") != canonical_state_hash(state)
    ):
        raise ColonyPlanError("The plan leaf source does not match the committed decision frame.")
    return context


def _authorizing_frame_id(bridge_dir: pathlib.Path, state: Dict[str, Any]) -> str:
    """Compatibility wrapper for callers that only need the validated id."""

    return _authorizing_decision_context(bridge_dir, state).frame_id


def _evaluate_condition(
    state: Dict[str, Any],
    condition: Dict[str, Any],
    *,
    action: Optional[str] = None,
) -> bool:
    if condition_contract_blockers(state, condition, action=action):
        return False
    marker = object()
    actual = safe_get(state, str(condition["path"]), marker)
    if actual is marker:
        return False
    expected = condition.get("value")
    operator = condition.get("op")
    try:
        if operator == "eq":
            return _strict_json_equal(actual, expected)
        if operator == "ne":
            return not _strict_json_equal(actual, expected)
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
        if operator == "contains":
            return isinstance(actual, list) and any(
                _strict_json_equal(item, expected) for item in actual
            )
    except (TypeError, ValueError):
        return False
    return False


def _strict_json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_strict_json_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and set(left) == set(right)
            and all(_strict_json_equal(left[key], right[key]) for key in left)
        )
    return left == right


def _binding_errors(plan: Dict[str, Any], state: Dict[str, Any], health: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for field in ("session_id", "save_id_hash", "world_id"):
        if state.get(field) != plan.get(field):
            errors.append(f"{field}_mismatch")
    age = snapshot_age_seconds(state)
    if health.get("status") != "ok" or safe_get(state, "bridge.stale", False) is True:
        errors.append("bridge_not_fresh")
    if age is None or age > stale_after_seconds(state):
        errors.append("snapshot_not_fresh")
    checkpoint = plan.get("last_checkpoint") if isinstance(plan.get("last_checkpoint"), dict) else {}
    effects = checkpoint.get("expected_effects") if isinstance(checkpoint.get("expected_effects"), list) else []
    if effects and not all(_evaluate_condition(state, item) for item in effects if isinstance(item, dict)):
        errors.append("checkpoint_state_mismatch")
    return sorted(set(errors))


def _step_by_id(plan: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    for step in plan.get("steps") or []:
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    raise PlanRecoveryRequired(f"Plan step is missing: {step_id}")


def _command_locations(bridge_dir: pathlib.Path, command_id: str) -> List[Tuple[str, pathlib.Path, Dict[str, Any]]]:
    locations: List[Tuple[str, pathlib.Path, Dict[str, Any]]] = []
    for directory_name, directory in ensure_command_dirs(bridge_dir).items():
        for path in directory.glob("*.json"):
            try:
                command = _load_json(path)
            except ColonyPlanError:
                continue
            if str(command.get("id") or "") == command_id:
                locations.append((directory_name, path, command))
    return locations


def _plan_commands(bridge_dir: pathlib.Path, plan_id: str) -> List[Tuple[str, pathlib.Path, Dict[str, Any]]]:
    result: List[Tuple[str, pathlib.Path, Dict[str, Any]]] = []
    for directory_name, directory in ensure_command_dirs(bridge_dir).items():
        for path in directory.glob("*.json"):
            try:
                command = _load_json(path)
            except ColonyPlanError:
                continue
            source = command.get("source") if isinstance(command.get("source"), dict) else {}
            if source.get("kind") == PLAN_COMMAND_SOURCE and source.get("plan_id") == plan_id:
                result.append((directory_name, path, command))
    return result


def _abort_blocking_command_ids(
    commands: Iterable[Tuple[str, pathlib.Path, Dict[str, Any]]],
) -> List[str]:
    return [
        str(command.get("id"))
        for directory_name, _path, command in commands
        if directory_name in ABORT_BLOCKING_STAGES
    ]


def _command_matches_step(plan: Dict[str, Any], step: Dict[str, Any], command: Dict[str, Any]) -> bool:
    source = command.get("source") if isinstance(command.get("source"), dict) else {}
    return (
        command.get("schema") == COMMAND_SCHEMA
        and command.get("id") == _leaf_command_id(str(plan.get("plan_id")), str(step.get("id")))
        and command.get("action") == step.get("action")
        and _strict_json_equal(command.get("args"), step.get("args"))
        and _strict_json_equal(command.get("preconditions"), step.get("preconditions"))
        and _strict_json_equal(command.get("expected_effects"), step.get("expected_effects"))
        and command.get("requires_approval") is True
        and command.get("session_id") == plan.get("session_id")
        and command.get("save_id_hash") == plan.get("save_id_hash")
        and command.get("world_id") == plan.get("world_id")
        and isinstance(command.get("source_frame_id"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(command.get("source_frame_id"))) is not None
        and source.get("kind") == PLAN_COMMAND_SOURCE
        and source.get("plan_id") == plan.get("plan_id")
        and source.get("step_id") == step.get("id")
    )


def _mark_ambiguous(plan: Dict[str, Any], reason: str, details: Optional[Dict[str, Any]] = None) -> None:
    if plan.get("status") not in TERMINAL_PLAN_STATUSES:
        plan["status"] = "paused"
    plan["pause_reason"] = reason
    _append_history(plan, "recovery_paused", {"reason": reason, **(details or {})})


def _reconcile_open_command(bridge_dir: pathlib.Path, plan: Dict[str, Any]) -> bool:
    command_id = plan.get("open_command_id")
    if not command_id:
        candidates: List[Tuple[str, pathlib.Path, Dict[str, Any]]] = []
        for item in _plan_commands(bridge_dir, str(plan["plan_id"])):
            source = item[2].get("source") if isinstance(item[2].get("source"), dict) else {}
            step_id = source.get("step_id")
            if not isinstance(step_id, str):
                continue
            try:
                candidate_step = _step_by_id(plan, step_id)
            except PlanRecoveryRequired:
                _mark_ambiguous(plan, "orphan_command_step_missing", {"step_id": step_id})
                return True
            if candidate_step.get("status") in {
                "waiting",
                "awaiting_approval",
                "awaiting_confirmation",
            }:
                candidates.append(item)
        if len(candidates) > 1:
            _mark_ambiguous(plan, "multiple_open_leaf_commands")
            return True
        if len(candidates) == 1:
            source = candidates[0][2].get("source") or {}
            step = _step_by_id(plan, str(source.get("step_id")))
            plan["open_command_id"] = candidates[0][2].get("id")
            plan["current_step"] = step["id"]
            step["command_id"] = candidates[0][2].get("id")
            step["status"] = "awaiting_approval"
            if plan.get("status") == "draft":
                plan["status"] = "active"
            command_id = plan["open_command_id"]
        else:
            return False

    locations = _command_locations(bridge_dir, str(command_id))
    if len(locations) != 1:
        _mark_ambiguous(
            plan,
            "open_command_location_ambiguous",
            {"command_id": command_id, "location_count": len(locations)},
        )
        return True
    directory_name, _, command = locations[0]
    source = command.get("source") if isinstance(command.get("source"), dict) else {}
    step_id = str(source.get("step_id") or plan.get("current_step") or "")
    step = _step_by_id(plan, step_id)
    if not _command_matches_step(plan, step, command):
        _mark_ambiguous(plan, "open_command_binding_mismatch", {"command_id": command_id})
        return True
    if directory_name in {"pending", "approved"}:
        return False
    if directory_name in {"confirming", "done"}:
        outcome, outcome_errors = read_action_outcome(bridge_dir, str(command_id))
        if outcome_errors or outcome is None:
            changed = step.get("status") != "awaiting_confirmation"
            step["status"] = "awaiting_confirmation"
            if changed:
                _append_history(
                    plan,
                    "leaf_execution_verified_awaiting_snapshot",
                    {"step_id": step["id"], "command_id": command_id, "blockers": outcome_errors},
                )
            return changed
        confirmation_status = outcome.get("confirmation_status")
        if confirmation_status == CONFIRMATION_REJECTED:
            _mark_ambiguous(
                plan,
                "leaf_snapshot_attestation_rejected",
                {"command_id": command_id, "blockers": list(outcome.get("blockers") or [])},
            )
            return True
        if confirmation_status != CONFIRMATION_CONFIRMED:
            changed = step.get("status") != "awaiting_confirmation"
            step["status"] = "awaiting_confirmation"
            if changed:
                _append_history(
                    plan,
                    "leaf_execution_verified_awaiting_snapshot",
                    {
                        "step_id": step["id"],
                        "command_id": command_id,
                        "blockers": list(outcome.get("blockers") or []),
                    },
                )
            return changed
        if directory_name != "done":
            changed = step.get("status") != "awaiting_confirmation"
            step["status"] = "awaiting_confirmation"
            if changed:
                _append_history(
                    plan,
                    "leaf_execution_verified_awaiting_snapshot",
                    {
                        "step_id": step["id"],
                        "command_id": command_id,
                        "blockers": ["confirmation_not_committed_to_done"],
                    },
                )
            return changed
        commit_blockers = confirmed_command_commit_blockers(command, outcome)
        if commit_blockers:
            _mark_ambiguous(
                plan,
                "leaf_done_confirmation_mismatch",
                {"command_id": command_id, "blockers": commit_blockers},
            )
            return True
        witness = outcome.get("witness") if isinstance(outcome.get("witness"), dict) else {}
        witness_hash = witness.get("state_hash")
        witness_snapshot_id = witness.get("snapshot_id")
        witness_seq = witness.get("seq")
        if (
            not isinstance(witness_hash, str)
            or not witness_hash.startswith("sha256:")
            or len(witness_hash) != 71
            or not isinstance(witness_snapshot_id, str)
            or isinstance(witness_seq, bool)
            or not isinstance(witness_seq, int)
            or witness_seq <= 0
        ):
            _mark_ambiguous(plan, "leaf_attestation_witness_invalid", {"command_id": command_id})
            return True
        effects = step.get("expected_effects") if isinstance(step.get("expected_effects"), list) else []
        step["status"] = "completed"
        step["completed_at"] = command.get("completed_at") or now_iso()
        step["result_state_hash"] = witness_hash
        plan["last_checkpoint"] = {
            "step_id": step["id"],
            "command_id": command_id,
            "completed_at": step["completed_at"],
            "snapshot_id": witness_snapshot_id,
            "seq": witness_seq,
            "state_hash": step["result_state_hash"],
            "attestation_hash": outcome.get("attestation_hash"),
            "expected_effects": copy.deepcopy(effects),
        }
        plan["open_command_id"] = None
        plan["current_step"] = None
        plan["pause_reason"] = None
        _append_history(
            plan,
            "leaf_snapshot_confirmed",
            {
                "step_id": step["id"],
                "command_id": command_id,
                "snapshot_id": witness_snapshot_id,
                "seq": witness_seq,
                "attestation_hash": outcome.get("attestation_hash"),
            },
        )
        if all(item.get("status") == "completed" for item in plan.get("steps") or []):
            plan["status"] = "completed"
            plan["completed_at"] = now_iso()
        return True
    if directory_name == "failed":
        step["status"] = "failed"
        plan["status"] = "failed"
        plan["failed_at"] = now_iso()
        plan["open_command_id"] = None
        plan["current_step"] = None
        _append_history(plan, "leaf_failed", {"step_id": step["id"], "command_id": command_id})
        return True
    if directory_name in {"rejected", "quarantine"}:
        step["status"] = "blocked"
        plan["status"] = "paused"
        plan["pause_reason"] = f"leaf_command_{directory_name}"
        plan["open_command_id"] = None
        plan["current_step"] = None
        _append_history(
            plan,
            "leaf_blocked",
            {"step_id": step["id"], "command_id": command_id, "directory": directory_name},
        )
        return True
    _mark_ambiguous(plan, "unknown_command_directory", {"command_id": command_id})
    return True


def _leaf_command_id(plan_id: str, step_id: str) -> str:
    digest = hashlib.sha256(f"{plan_id}:{step_id}".encode("utf-8")).hexdigest()[:20]
    return f"cmd-plan-{digest}"


def _build_leaf_command(
    bridge_dir: pathlib.Path,
    plan: Dict[str, Any],
    step: Dict[str, Any],
    state: Dict[str, Any],
    *,
    decision_context: CurrentDecisionContext,
) -> Dict[str, Any]:
    created = now()
    policy = decision_context.policy
    if (
        policy.get("schema") != "oni.action-policy.v1"
        or policy.get("actions_enabled") is not True
        or step.get("action") not in list(policy.get("allowed_actions") or [])
        or state.get("save_id_hash") not in list(policy.get("allowed_save_hashes") or [])
    ):
        raise ColonyPlanError("The current action policy does not authorize this plan leaf.")
    try:
        ttl_seconds = max(1, int(policy.get("command_ttl_seconds") or 30))
    except (TypeError, ValueError):
        ttl_seconds = 30
    command_id = _leaf_command_id(str(plan["plan_id"]), str(step["id"]))
    return {
        "schema": COMMAND_SCHEMA,
        "id": command_id,
        "mode": "planned",
        "status": "pending_human_approval",
        "action": step["action"],
        "args": copy.deepcopy(step["args"]),
        "session_id": state.get("session_id"),
        "save_id_hash": state.get("save_id_hash"),
        "world_id": state.get("world_id"),
        "source_incident_id": None,
        "source_snapshot_id": state.get("snapshot_id"),
        "state_hash": canonical_state_hash(state),
        "source_frame_id": decision_context.frame_id,
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
        "risk": step.get("risk"),
        "reason": step.get("reason") or f"Colony plan {plan['plan_id']} step {step['id']}",
        "evidence": {
            "plan_id": plan["plan_id"],
            "plan_revision": plan["revision"],
            "step_id": step["id"],
        },
        "preconditions": copy.deepcopy(step["preconditions"]),
        "expected_effects": copy.deepcopy(step["expected_effects"]),
        "requires_approval": True,
        "source": {
            "kind": PLAN_COMMAND_SOURCE,
            "plan_id": plan["plan_id"],
            "plan_revision": plan["revision"],
            "step_id": step["id"],
            "state_seq": state.get("seq"),
            "state_ts": state.get("ts"),
            "policy_hash": decision_context.policy_hash,
        },
    }


def _ready_step(plan: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    completed = {
        str(step.get("id"))
        for step in plan.get("steps") or []
        if isinstance(step, dict) and step.get("status") == "completed"
    }
    cycle = state.get("cycle")
    cycle_observed = field_quality(state, "cycle").get("status") == "observed"
    for step in plan.get("steps") or []:
        if step.get("status") != "waiting" or not set(step.get("dependencies") or []).issubset(completed):
            continue
        not_before = step.get("not_before_cycle")
        not_after = step.get("not_after_cycle")
        if not_before is not None or not_after is not None:
            if not cycle_observed or isinstance(cycle, bool) or not isinstance(cycle, (int, float)):
                continue
            if not_before is not None and cycle < not_before:
                continue
            if not_after is not None and cycle > not_after:
                step["status"] = "blocked"
                plan["status"] = "paused"
                plan["pause_reason"] = "cycle_window_expired"
                _append_history(plan, "step_blocked", {"step_id": step["id"], "reason": "cycle_window_expired"})
                return None
        return step
    return None


def _pause_and_quarantine_context_changed(
    bridge_dir: pathlib.Path,
    plan: Dict[str, Any],
    step: Dict[str, Any],
    command: Dict[str, Any],
    *,
    context_error: Optional[BaseException] = None,
) -> None:
    """Persist a blocked leaf before attempting any custody isolation."""

    command_id = str(command["id"])
    step_id = str(step["id"])
    step["status"] = "blocked"
    step["command_id"] = command_id
    plan["status"] = "paused"
    plan["current_step"] = step_id
    plan["open_command_id"] = command_id
    plan["pause_reason"] = "leaf_publication_context_changed"
    _append_history(
        plan,
        "leaf_publication_context_changed",
        {"step_id": step_id, "command_id": command_id},
    )
    # This is the first and only mutation attempted before custody isolation.
    # If it fails, leave the command and the in-memory evidence untouched.
    plan = _commit_revision(bridge_dir, plan, "advance_colony_plan_context_changed")

    try:
        locations = _command_locations(bridge_dir, command_id)
        pending = [item for item in locations if item[0] == "pending"]
        if len(pending) != 1:
            raise CommandCustodyUnknown(
                "The context-changed leaf is not uniquely present in pending custody."
            )
        _quarantine_pending_command(
            bridge_dir,
            pending[0][1],
            command,
            str(plan["plan_id"]),
            reason="colony_plan_decision_context_changed",
        )
    except Exception as exc:
        raise PlanRecoveryRequired(
            "The plan was paused before context-change isolation, but command custody is unresolved; "
            "manual recovery is required."
        ) from exc

    plan["open_command_id"] = None
    plan["current_step"] = None
    _append_history(
        plan,
        "leaf_command_quarantined_after_context_change",
        {
            "step_id": step_id,
            "command_id": command_id,
            "quarantine_reason": "colony_plan_decision_context_changed",
        },
    )
    _commit_revision(bridge_dir, plan, "advance_colony_plan_context_quarantined")
    message = (
        "The plan leaf decision context changed during publication; the plan is paused and the command "
        "is quarantined. Abort this plan and create a reviewed plan before retrying."
    )
    if context_error is not None:
        raise ColonyPlanError(message) from context_error
    raise ColonyPlanError(message)


def create_colony_plan(bridge_dir: pathlib.Path, definition: Dict[str, Any]) -> Dict[str, Any]:
    bridge_dir = bridge_dir.expanduser().resolve()
    title, goal, steps = _validate_definition(definition)
    decision_provenance = _validated_decision_provenance(definition)
    with _plan_lock(bridge_dir):
        state, health = _load_live_state(bridge_dir)
        probe = {
            "session_id": state.get("session_id"),
            "save_id_hash": state.get("save_id_hash"),
            "world_id": state.get("world_id"),
            "last_checkpoint": {},
        }
        errors = _binding_errors(probe, state, health)
        if errors:
            raise ColonyPlanError("Cannot create a plan from an untrusted live state: " + ", ".join(errors))
        if decision_provenance and isinstance(decision_provenance.get("source"), dict):
            source = decision_provenance["source"]
            expected_source = {
                "session_id": state.get("session_id"),
                "save_id_hash": state.get("save_id_hash"),
                "world_id": state.get("world_id"),
                "snapshot_id": state.get("snapshot_id"),
                "state_hash": canonical_state_hash(state),
            }
            if any(source.get(key) != value for key, value in expected_source.items() if key in source):
                raise ColonyPlanError("The planner source snapshot no longer matches the live state.")
        pointer_path = plans_root(bridge_dir) / "active.json"
        if pointer_path.exists():
            try:
                existing = load_active_plan(bridge_dir)
            except ColonyPlanError as exc:
                raise PlanRecoveryRequired("Recover the existing colony plan before creating another.") from exc
            if existing.get("status") not in TERMINAL_PLAN_STATUSES:
                raise ColonyPlanError("A non-terminal colony plan already exists.")
        plan_id = "plan-" + now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
        created_at = now_iso()
        plan: Dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "plan_id": plan_id,
            "revision": 0,
            "revision_hash": None,
            "previous_revision_hash": None,
            "title": title,
            "goal": goal,
            "status": "draft",
            "session_id": state.get("session_id"),
            "save_id_hash": state.get("save_id_hash"),
            "world_id": state.get("world_id"),
            "source_snapshot_id": state.get("snapshot_id"),
            "state_hash": canonical_state_hash(state),
            "created_at": created_at,
            "updated_at": created_at,
            "current_step": None,
            "open_command_id": None,
            "pause_reason": None,
            "last_checkpoint": {
                "step_id": None,
                "command_id": None,
                "completed_at": None,
                "snapshot_id": state.get("snapshot_id"),
                "state_hash": canonical_state_hash(state),
                "expected_effects": [],
            },
            "steps": steps,
            "decision_provenance": decision_provenance,
            "history": [],
        }
        _append_history(plan, "create_colony_plan", {"source_snapshot_id": state.get("snapshot_id")})
        return _commit_revision(bridge_dir, plan, "create_colony_plan")


def plan_leaf_contract_hash(step: Dict[str, Any]) -> str:
    """Bind the exact selected leaf without its changing lifecycle fields."""
    return canonical_document_hash({key: step.get(key) for key in (
        "id", "action", "args", "preconditions", "expected_effects",
    )})


def advance_colony_plan(
    bridge_dir: pathlib.Path, plan_id: Optional[str] = None, *,
    expected_step_id: Optional[str] = None,
    expected_state_hash: Optional[str] = None,
    expected_step_hash: Optional[str] = None,
) -> Dict[str, Any]:
    guarded = any(value is not None for value in (expected_step_id, expected_state_hash, expected_step_hash))
    if guarded and (not isinstance(expected_step_id, str) or not expected_step_id
                    or not isinstance(expected_state_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_state_hash) is None
                    or not isinstance(expected_step_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_step_hash) is None):
        raise ColonyPlanError("A guarded advance requires an exact step and both hashes.")
    bridge_dir = bridge_dir.expanduser().resolve()
    with _plan_lock(bridge_dir):
        plan = copy.deepcopy(load_active_plan(bridge_dir, plan_id))
        if plan.get("status") not in {"draft", "active"}:
            raise ColonyPlanError("Only draft or active plans can advance.")
        if guarded and plan.get("open_command_id") is not None:
            raise ColonyPlanError("A guarded advance cannot reconcile or replay an existing command.")
        reconciled = False if guarded else _reconcile_open_command(bridge_dir, plan)
        if plan.get("status") in TERMINAL_PLAN_STATUSES or plan.get("status") == "paused":
            return _commit_revision(bridge_dir, plan, "advance_colony_plan_reconcile") if reconciled else plan
        if plan.get("open_command_id"):
            if reconciled:
                return _commit_revision(bridge_dir, plan, "advance_colony_plan_reconcile")
            return plan

        state, health = _load_live_state(bridge_dir)
        if guarded and canonical_state_hash(state) != expected_state_hash:
            raise ColonyPlanError("The guarded advance source state changed.")
        errors = _binding_errors(plan, state, health)
        if errors:
            if plan.get("status") == "active":
                _mark_ambiguous(plan, "live_binding_failed", {"errors": errors})
                return _commit_revision(bridge_dir, plan, "advance_colony_plan_paused")
            raise ColonyPlanError("Draft plan binding failed: " + ", ".join(errors))
        step = _ready_step(plan, state)
        if guarded and (step is None or step.get("id") != expected_step_id
                        or plan_leaf_contract_hash(step) != expected_step_hash):
            raise ColonyPlanError("The guarded advance selected a different leaf.")
        if step is None:
            if plan.get("status") == "paused":
                return _commit_revision(bridge_dir, plan, "advance_colony_plan_paused")
            if all(item.get("status") == "completed" for item in plan.get("steps") or []):
                plan["status"] = "completed"
                plan["completed_at"] = now_iso()
                _append_history(plan, "plan_completed")
                return _commit_revision(bridge_dir, plan, "advance_colony_plan_completed")
            return plan
        failed_preconditions = [
            item
            for item in step.get("preconditions") or []
            if not _evaluate_condition(state, item, action=str(step.get("action") or ""))
        ]
        if failed_preconditions:
            if plan.get("status") == "active":
                _mark_ambiguous(
                    plan,
                    "step_precondition_failed",
                    {"step_id": step["id"], "paths": [item.get("path") for item in failed_preconditions]},
                )
                return _commit_revision(bridge_dir, plan, "advance_colony_plan_paused")
            raise ColonyPlanError(f"Draft step {step['id']} preconditions do not match the current state.")

        decision_context = _authorizing_decision_context(bridge_dir, state)
        pipeline = command_pipeline_readiness(
            bridge_dir,
            latest_state=state,
            proposal_frame=decision_context.frame,
            policy=decision_context.policy,
        )
        readiness = pipeline.get("readiness") if isinstance(pipeline.get("readiness"), dict) else {}
        proposal = (
            readiness.get("mutation_proposal")
            if isinstance(readiness.get("mutation_proposal"), dict)
            else {}
        )
        blockers = list(proposal.get("blockers") or [])
        if proposal.get("ready") is not True or blockers:
            raise ColonyPlanError(
                "Cannot emit a colony-plan leaf while the command pipeline is blocked: "
                + ", ".join(str(item) for item in blockers or ["pipeline_readiness_invalid"])
            )

        command = _build_leaf_command(
            bridge_dir,
            plan,
            step,
            state,
            decision_context=decision_context,
        )
        locations = _command_locations(bridge_dir, str(command["id"]))
        if locations:
            _mark_ambiguous(plan, "leaf_command_id_already_exists", {"command_id": command["id"]})
            return _commit_revision(bridge_dir, plan, "advance_colony_plan_paused")
        try:
            assert_current_decision_context(decision_context)
        except DecisionFrameArchiveError as exc:
            raise ColonyPlanError("The plan leaf decision context changed before publication.") from exc
        try:
            publish_pending_command(bridge_dir, command)
        except CommandCustodyError as exc:
            raise ColonyPlanError("The plan leaf could not be published under command custody.") from exc
        try:
            assert_current_decision_context(decision_context)
        except DecisionFrameArchiveError as exc:
            _pause_and_quarantine_context_changed(
                bridge_dir,
                plan,
                step,
                command,
                context_error=exc,
            )
        step["status"] = "awaiting_approval"
        step["command_id"] = command["id"]
        plan["status"] = "active"
        plan["current_step"] = step["id"]
        plan["open_command_id"] = command["id"]
        plan["pause_reason"] = None
        _append_history(
            plan,
            "advance_colony_plan",
            {"step_id": step["id"], "command_id": command["id"], "requires_approval": True},
        )
        return _commit_revision(bridge_dir, plan, "advance_colony_plan")


def reconcile_colony_plan_leaf(
    bridge_dir: pathlib.Path,
    plan_id: Optional[str] = None,
    *,
    expected_step_id: str,
    expected_command_id: str,
    expected_witness_snapshot_id: Optional[str] = None,
    expected_witness_seq: Optional[int] = None,
    expected_witness_state_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Reconcile one already terminal leaf without selecting or publishing another.

    This is deliberately separate from ``advance_colony_plan``: the latter is
    allowed to choose the next ready leaf, while a controller confirmation must
    first persist exactly the leaf it observed. Queue and outcome validation is
    repeated under the shared lifecycle custody before the revision is written.
    """
    if not isinstance(expected_step_id, str) or not SAFE_ID.fullmatch(expected_step_id):
        raise ColonyPlanError("The expected plan step id is invalid.")
    if not isinstance(expected_command_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", expected_command_id):
        raise ColonyPlanError("The expected command id is invalid.")
    witness_expectations_supplied = any(value is not None for value in (
        expected_witness_snapshot_id, expected_witness_seq, expected_witness_state_hash,
    ))
    if witness_expectations_supplied and (
        not isinstance(expected_witness_snapshot_id, str) or not expected_witness_snapshot_id
        or type(expected_witness_seq) is not int or expected_witness_seq <= 0
        or not isinstance(expected_witness_state_hash, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_witness_state_hash) is None
    ):
        raise ColonyPlanError("The expected confirmation witness is invalid.")
    bridge_dir = bridge_dir.expanduser().resolve()
    with _plan_lock(bridge_dir):
        plan = copy.deepcopy(load_active_plan(bridge_dir, plan_id))
        if plan.get("open_command_id") != expected_command_id or plan.get("current_step") != expected_step_id:
            raise ColonyPlanError("The plan open leaf does not match the confirmed controller command.")
        step = _step_by_id(plan, expected_step_id)
        if step.get("command_id") != expected_command_id:
            raise ColonyPlanError("The plan leaf command binding does not match the confirmed command.")
        before_revision = plan.get("revision")
        if witness_expectations_supplied:
            outcome, outcome_errors = read_action_outcome(bridge_dir, expected_command_id)
            if outcome_errors or outcome is None:
                raise ColonyPlanError("The confirmation witness is unavailable.")
            witness = outcome.get("witness") if isinstance(outcome.get("witness"), dict) else {}
            if (
                witness.get("snapshot_id") != expected_witness_snapshot_id
                or witness.get("seq") != expected_witness_seq
                or witness.get("state_hash") != expected_witness_state_hash
            ):
                raise ColonyPlanError("The confirmation witness does not match the observed effect.")
        changed = _reconcile_open_command(bridge_dir, plan)
        if not changed:
            raise ColonyPlanError("The selected leaf has not reached a terminal confirmation.")
        if plan.get("open_command_id") is not None:
            raise ColonyPlanError("The selected leaf was not reconciled to a terminal state.")
        reconciled = _step_by_id(plan, expected_step_id)
        if reconciled.get("status") != "completed":
            raise ColonyPlanError("The selected leaf did not receive a confirmed checkpoint.")
        result = _commit_revision(bridge_dir, plan, "reconcile_colony_plan_leaf")
        if result.get("revision", 0) != int(before_revision or 0) + 1:
            raise PlanRecoveryRequired("The selected leaf reconciliation revision was not monotonic.")
        return result


def mark_colony_plan_leaf_already_satisfied(
    bridge_dir: pathlib.Path, plan_id: Optional[str] = None, *,
    expected_step_id: str, expected_state_hash: str,
) -> Dict[str, Any]:
    """Complete one waiting leaf from a fresh satisfied state, without a command."""
    if not isinstance(expected_step_id, str) or not SAFE_ID.fullmatch(expected_step_id):
        raise ColonyPlanError("The expected plan step id is invalid.")
    if not isinstance(expected_state_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_state_hash) is None:
        raise ColonyPlanError("The expected satisfied-state hash is invalid.")
    bridge_dir = bridge_dir.expanduser().resolve()
    with _plan_lock(bridge_dir):
        plan = copy.deepcopy(load_active_plan(bridge_dir, plan_id))
        if plan.get("open_command_id") is not None or plan.get("current_step") is not None:
            raise ColonyPlanError("An open command prevents no-op leaf completion.")
        step = _step_by_id(plan, expected_step_id)
        if step.get("status") != "waiting":
            raise ColonyPlanError("Only a waiting leaf can be marked already satisfied.")
        state, health = _load_live_state(bridge_dir)
        if canonical_state_hash(state) != expected_state_hash or _binding_errors(plan, state, health):
            raise ColonyPlanError("The satisfied-state binding is stale or untrusted.")
        effects = step.get("expected_effects") if isinstance(step.get("expected_effects"), list) else []
        if not effects or not all(_evaluate_condition(state, item, action=str(step.get("action") or "")) for item in effects):
            raise ColonyPlanError("The selected leaf effects are not currently satisfied.")
        step["status"] = "completed"
        step["completion_mode"] = "already_satisfied_without_command"
        step["completed_at"] = now_iso()
        step["result_state_hash"] = expected_state_hash
        plan["last_checkpoint"] = {"step_id": expected_step_id, "command_id": None,
            "completed_at": step["completed_at"], "snapshot_id": state.get("snapshot_id"),
            "seq": state.get("seq"), "state_hash": expected_state_hash,
            "expected_effects": copy.deepcopy(effects),
            "completion_mode": "already_satisfied_without_command"}
        _append_history(plan, "leaf_already_satisfied", {"step_id": expected_step_id,
            "state_hash": expected_state_hash, "completion_mode": "already_satisfied_without_command"})
        if all(item.get("status") == "completed" for item in plan.get("steps") or []):
            plan["status"] = "completed"
            plan["completed_at"] = now_iso()
        return _commit_revision(bridge_dir, plan, "mark_colony_plan_leaf_already_satisfied")


def pause_colony_plan(bridge_dir: pathlib.Path, plan_id: Optional[str] = None) -> Dict[str, Any]:
    bridge_dir = bridge_dir.expanduser().resolve()
    with _plan_lock(bridge_dir):
        plan = copy.deepcopy(load_active_plan(bridge_dir, plan_id))
        if plan.get("status") != "active":
            raise ColonyPlanError("Only an active plan can be paused.")
        _reconcile_open_command(bridge_dir, plan)
        if plan.get("status") in TERMINAL_PLAN_STATUSES:
            return _commit_revision(bridge_dir, plan, "pause_colony_plan_reconcile")
        plan["status"] = "paused"
        plan["pause_reason"] = "manual_pause"
        _append_history(plan, "pause_colony_plan", {"open_command_id": plan.get("open_command_id")})
        return _commit_revision(bridge_dir, plan, "pause_colony_plan")


def resume_colony_plan(bridge_dir: pathlib.Path, plan_id: Optional[str] = None) -> Dict[str, Any]:
    bridge_dir = bridge_dir.expanduser().resolve()
    with _plan_lock(bridge_dir):
        plan = copy.deepcopy(load_active_plan(bridge_dir, plan_id))
        if plan.get("status") != "paused":
            raise ColonyPlanError("Only a paused plan can be resumed.")
        _reconcile_open_command(bridge_dir, plan)
        if plan.get("status") in TERMINAL_PLAN_STATUSES:
            return _commit_revision(bridge_dir, plan, "resume_colony_plan_reconcile")
        if any(step.get("status") in {"blocked", "failed"} for step in plan.get("steps") or []):
            raise ColonyPlanError("A plan with blocked or failed steps cannot resume; abort and create a reviewed revision.")
        state, health = _load_live_state(bridge_dir)
        errors = _binding_errors(plan, state, health)
        if errors:
            plan["pause_reason"] = "resume_revalidation_failed"
            _append_history(plan, "resume_rejected", {"errors": errors})
            return _commit_revision(bridge_dir, plan, "resume_colony_plan_rejected")
        plan["status"] = "active"
        plan["pause_reason"] = None
        _append_history(plan, "resume_colony_plan", {"open_command_id": plan.get("open_command_id")})
        return _commit_revision(bridge_dir, plan, "resume_colony_plan")


def _quarantine_pending_command(
    bridge_dir: pathlib.Path,
    path: pathlib.Path,
    command: Dict[str, Any],
    plan_id: str,
    *,
    reason: str = "colony_plan_aborted",
) -> pathlib.Path:
    payload = copy.deepcopy(command)
    payload.update(
        {
            "status": "quarantined",
            "quarantine_reason": reason,
            "quarantined_at": now_iso(),
        }
    )
    if reason == "colony_plan_aborted":
        payload["aborted_plan_id"] = plan_id
    else:
        payload.pop("aborted_plan_id", None)
    try:
        with lifecycle_custody(bridge_dir):
            dirs = ensure_command_dirs(bridge_dir)
            target = dirs["quarantine"] / path.name
            observation = observe_pending_envelope(
                bridge_dir,
                path,
                expected_source=command,
            )
            if observation is None:
                raise CommandCustodyUnknown(
                    "The pending source disappeared before it could be observed for quarantine."
                )
            result = transition_pending_envelope(
                bridge_dir,
                path,
                target,
                payload,
                expected_source=command,
                expected_observation=observation,
                operation="quarantine",
            )
            if result is None:
                raise CommandCustodyUnknown(
                    "The pending source was absent during quarantine; custody success is not proven."
                )
            return result
    except CommandCustodyError as exc:
        raise ColonyPlanError(
            "Pending leaf custody failed closed; the plan remains non-terminal."
        ) from exc


def abort_colony_plan(bridge_dir: pathlib.Path, plan_id: Optional[str] = None) -> Dict[str, Any]:
    bridge_dir = bridge_dir.expanduser().resolve()
    with _plan_lock(bridge_dir):
        plan = copy.deepcopy(load_active_plan(bridge_dir, plan_id))
        if plan.get("status") not in {"draft", "active", "paused"}:
            raise ColonyPlanError("Only draft, active, or paused plans can be aborted.")
        commands = _plan_commands(bridge_dir, str(plan["plan_id"]))
        blocking = _abort_blocking_command_ids(commands)
        if blocking:
            raise ColonyPlanError(
                "Cannot abort while a leaf command may still execute: " + ", ".join(blocking)
            )
        _reconcile_open_command(bridge_dir, plan)
        commands = _plan_commands(bridge_dir, str(plan["plan_id"]))
        blocking = _abort_blocking_command_ids(commands)
        if blocking:
            raise ColonyPlanError(
                "Cannot abort while a leaf command may still execute: " + ", ".join(blocking)
            )
        if plan.get("status") in TERMINAL_PLAN_STATUSES:
            return _commit_revision(bridge_dir, plan, "abort_colony_plan_reconcile")
        quarantined: List[str] = []
        for directory, path, command in commands:
            if directory == "pending":
                _quarantine_pending_command(bridge_dir, path, command, str(plan["plan_id"]))
                quarantined.append(str(command.get("id")))
        blocking_after_claim = _abort_blocking_command_ids(
            _plan_commands(bridge_dir, str(plan["plan_id"]))
        )
        if blocking_after_claim:
            raise ColonyPlanError(
                "Cannot abort because a leaf command became executable during termination: "
                + ", ".join(blocking_after_claim)
            )
        for step in plan.get("steps") or []:
            if step.get("status") in {
                "waiting",
                "awaiting_approval",
                "awaiting_confirmation",
                "blocked",
            }:
                step["status"] = "cancelled"
        plan["status"] = "aborted"
        plan["aborted_at"] = now_iso()
        plan["open_command_id"] = None
        plan["current_step"] = None
        plan["pause_reason"] = None
        _append_history(plan, "abort_colony_plan", {"quarantined_command_ids": quarantined})
        return _commit_revision(bridge_dir, plan, "abort_colony_plan")


def _resolve_plan_id_for_recovery(bridge_dir: pathlib.Path, plan_id: Optional[str]) -> str:
    if plan_id is not None:
        if not SAFE_PLAN_ID.fullmatch(plan_id):
            raise ColonyPlanError("Invalid colony plan id.")
        return plan_id
    try:
        pointer = _load_json(plans_root(bridge_dir) / "active.json")
        candidate = str(pointer.get("plan_id") or "")
        if SAFE_PLAN_ID.fullmatch(candidate):
            return candidate
    except ColonyPlanError:
        pass
    candidates = [path.name for path in plans_root(bridge_dir).glob("plan-*") if path.is_dir()]
    if len(candidates) != 1:
        raise ColonyPlanError("Specify --plan-id because the active pointer cannot identify one plan.")
    return candidates[0]


def _recover_revision_chain(bridge_dir: pathlib.Path, plan_id: str) -> Tuple[Dict[str, Any], List[str]]:
    directory = plan_directory(bridge_dir, plan_id)
    revision_paths = sorted((directory / "revisions").glob("[0-9][0-9][0-9][0-9][0-9][0-9].json"))
    if not revision_paths:
        raise ColonyPlanError("No immutable colony plan revisions are available for recovery.")
    expected_revision = 1
    previous_hash: Optional[str] = None
    latest: Optional[Dict[str, Any]] = None
    invalid_paths: List[pathlib.Path] = []
    for index, path in enumerate(revision_paths):
        try:
            value = _load_json(path)
        except ColonyPlanError:
            invalid_paths.extend(revision_paths[index:])
            break
        if (
            not _revision_valid(value)
            or value.get("plan_id") != plan_id
            or value.get("revision") != expected_revision
            or value.get("previous_revision_hash") != previous_hash
        ):
            invalid_paths.extend(revision_paths[index:])
            break
        latest = value
        previous_hash = str(value["revision_hash"])
        expected_revision += 1
    if latest is None:
        raise ColonyPlanError("The colony plan revision chain has no valid root revision.")
    archived: List[str] = []
    if invalid_paths:
        corrupt_dir = directory / "corrupt"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        stamp = now().strftime("%Y%m%d%H%M%S")
        for path in invalid_paths:
            target = corrupt_dir / f"{path.stem}-{stamp}.invalid.json"
            path.replace(target)
            archived.append(str(target))
    return copy.deepcopy(latest), archived


def recover_colony_plan(bridge_dir: pathlib.Path, plan_id: Optional[str] = None) -> Dict[str, Any]:
    bridge_dir = bridge_dir.expanduser().resolve()
    with _plan_lock(bridge_dir):
        resolved_id = _resolve_plan_id_for_recovery(bridge_dir, plan_id)
        pointer_path = plans_root(bridge_dir) / "active.json"
        if pointer_path.exists():
            try:
                active_id = _active_plan_id(bridge_dir)
            except ColonyPlanError:
                active_id = None
            if active_id is not None and active_id != resolved_id:
                try:
                    active = load_active_plan(bridge_dir, active_id)
                except ColonyPlanError as exc:
                    raise PlanRecoveryRequired(
                        "Recover the pointed active plan before selecting another plan."
                    ) from exc
                if active.get("status") not in TERMINAL_PLAN_STATUSES:
                    raise ColonyPlanError("Cannot recover another plan while the active plan is non-terminal.")
        plan, archived = _recover_revision_chain(bridge_dir, resolved_id)
        repaired = {
            "schema": PLAN_POINTER_SCHEMA,
            "plan_id": plan["plan_id"],
            "revision": plan["revision"],
            "revision_hash": plan["revision_hash"],
            "status": plan["status"],
            "updated_at": now_iso(),
        }
        write_json_atomic(plan_directory(bridge_dir, resolved_id) / "current.json", plan)
        write_json_atomic(plans_root(bridge_dir) / "active.json", repaired)
        write_json_atomic(bridge_dir / "colony-plan.json", plan)

        reconciled = _reconcile_open_command(bridge_dir, plan)
        errors: List[str] = []
        if plan.get("status") not in TERMINAL_PLAN_STATUSES:
            try:
                state, health = _load_live_state(bridge_dir)
                errors = _binding_errors(plan, state, health)
            except ColonyPlanError as exc:
                errors = [str(exc)]
            if errors and plan.get("status") != "draft":
                _mark_ambiguous(plan, "recovery_revalidation_failed", {"errors": errors})
        _append_history(
            plan,
            "recover_colony_plan",
            {
                "reconciled": reconciled,
                "archived_corrupt_revisions": archived,
                "revalidation_errors": errors,
            },
        )
        return _commit_revision(bridge_dir, plan, "recover_colony_plan")


def plan_operation_result(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema": PLAN_SCHEMA,
        "plan_id": plan.get("plan_id"),
        "revision": plan.get("revision"),
        "revision_hash": plan.get("revision_hash"),
        "status": plan.get("status"),
        "current_step": plan.get("current_step"),
        "open_command_id": plan.get("open_command_id"),
        "pause_reason": plan.get("pause_reason"),
        "steps": [
            {
                "id": step.get("id"),
                "action": step.get("action"),
                "status": step.get("status"),
                "command_id": step.get("command_id"),
            }
            for step in plan.get("steps") or []
            if isinstance(step, dict)
        ],
    }
