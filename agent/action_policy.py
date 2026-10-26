"""Strict shared contract for local ONI action-policy documents.

The policy is an authorization input, so every Python control-plane consumer
must agree on one closed shape and the same numeric boundaries. The optional
``source`` member exists only in the copied Decision Frame input and is never
part of the on-disk policy hash.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

from agent.canonical_hash import CanonicalHashError, canonical_hash
from agent.state_quality import parse_snapshot_timestamp


ACTION_POLICY_SCHEMA = "oni.action-policy.v1"
AUTONOMOUS_ACTION_POLICY_SCHEMA = "oni.action-policy.v2"
AUTONOMOUS_EXECUTION_MODE = "autonomous"
MIN_COMMAND_TTL_SECONDS = 1
MAX_COMMAND_TTL_SECONDS = 300
MIN_PENDING_COMMANDS = 1
MAX_PENDING_COMMANDS = 100
MAX_POLICY_SAVE_HASHES = 256

ALLOWED_ACTIONS = frozenset(
    {
        "focus_note",
        "set_paused",
        "set_research",
        "set_duplicant_chore_priority",
        "mark_dig_cells",
        "cancel_dig_cells",
        "place_building_blueprint",
        "cancel_building_blueprint",
        "place_utility_blueprint_path",
        "cancel_utility_blueprint_path",
        "noop",
    }
)

_REQUIRED_KEYS = frozenset(
    {
        "schema",
        "actions_enabled",
        "suggestions_enabled",
        "allowed_actions",
        "allowed_save_hashes",
        "command_ttl_seconds",
        "max_pending_commands",
    }
)
_OPTIONAL_KEYS = frozenset({"generated_at"})
_AUTONOMOUS_KEYS = frozenset(
    {"execution_mode", "run_id", "controller_id", "session_id", "world_id"}
)
_SAVE_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AUTONOMOUS_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,160}$")


def autonomous_identifier_is_valid(value: Any) -> bool:
    return isinstance(value, str) and _AUTONOMOUS_IDENTIFIER_RE.fullmatch(value) is not None


def _exact_int(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def policy_document(
    value: Mapping[str, Any], *, allow_synthetic_source: bool = False
) -> Dict[str, Any]:
    document = dict(value)
    if allow_synthetic_source:
        document.pop("source", None)
    return document


def action_policy_validation_blockers(
    value: Any, *, allow_synthetic_source: bool = False
) -> List[str]:
    if not isinstance(value, Mapping):
        return ["policy_not_object"]
    document = policy_document(value, allow_synthetic_source=allow_synthetic_source)
    blockers: List[str] = []
    keys = set(document)
    autonomous = document.get("schema") == AUTONOMOUS_ACTION_POLICY_SCHEMA
    required_keys = _REQUIRED_KEYS | (_AUTONOMOUS_KEYS if autonomous else frozenset())
    blockers.extend(
        f"policy_field_missing:{key}" for key in sorted(required_keys - keys)
    )
    blockers.extend(
        f"policy_field_unknown:{key}"
        for key in sorted(keys - required_keys - _OPTIONAL_KEYS)
    )

    if document.get("schema") not in (ACTION_POLICY_SCHEMA, AUTONOMOUS_ACTION_POLICY_SCHEMA):
        blockers.append("policy_schema_invalid")
    if autonomous:
        if document.get("execution_mode") != AUTONOMOUS_EXECUTION_MODE:
            blockers.append("policy_execution_mode_invalid")
        for key in ("run_id", "controller_id"):
            if not autonomous_identifier_is_valid(document.get(key)):
                blockers.append(f"policy_{key}_invalid")
        session = document.get("session_id")
        if not isinstance(session, str) or _SESSION_RE.fullmatch(session) is None:
            blockers.append("policy_session_id_invalid")
        if not _exact_int(document.get("world_id"), 0, 2147483647):
            blockers.append("policy_world_id_invalid")
    for key in ("actions_enabled", "suggestions_enabled"):
        if not isinstance(document.get(key), bool):
            blockers.append(f"policy_{key}_invalid")

    actions = document.get("allowed_actions")
    if not isinstance(actions, list):
        blockers.append("policy_allowed_actions_invalid")
    elif (
        not actions
        or any(not isinstance(item, str) or item not in ALLOWED_ACTIONS for item in actions)
        or len(actions) != len(set(actions))
    ):
        blockers.append("policy_allowed_actions_invalid")

    saves = document.get("allowed_save_hashes")
    if not isinstance(saves, list):
        blockers.append("policy_allowed_save_hashes_invalid")
    elif (
        len(saves) > MAX_POLICY_SAVE_HASHES
        or any(
            not isinstance(item, str) or _SAVE_HASH_RE.fullmatch(item) is None
            for item in saves
        )
        or len(saves) != len(set(saves))
    ):
        blockers.append("policy_allowed_save_hashes_invalid")
    elif document.get("actions_enabled") is True and not saves:
        blockers.append("policy_enabled_without_save_hash")
    if autonomous and document.get("actions_enabled") is True:
        if not isinstance(saves, list) or len(saves) != 1:
            blockers.append("policy_autonomous_save_count_invalid")
        if type(document.get("max_pending_commands")) is not int or document.get("max_pending_commands") != 1:
            blockers.append("policy_autonomous_max_pending_invalid")

    if not _exact_int(
        document.get("command_ttl_seconds"),
        MIN_COMMAND_TTL_SECONDS,
        MAX_COMMAND_TTL_SECONDS,
    ):
        blockers.append("policy_command_ttl_invalid")
    if not _exact_int(
        document.get("max_pending_commands"),
        MIN_PENDING_COMMANDS,
        MAX_PENDING_COMMANDS,
    ):
        blockers.append("policy_max_pending_invalid")

    if (
        "generated_at" in document
        and parse_snapshot_timestamp(document.get("generated_at")) is None
    ):
        blockers.append("policy_generated_at_invalid")
    return sorted(set(blockers))


def action_policy_is_valid(
    value: Any, *, allow_synthetic_source: bool = False
) -> bool:
    return not action_policy_validation_blockers(
        value, allow_synthetic_source=allow_synthetic_source
    )


def autonomous_policy_state_blockers(policy: Any, state: Any) -> List[str]:
    """Bind autonomous authority to actual Probe startup and save identity.

    The run identity is a top-level ProbeSession field. It is never taken from
    a model choice, an inferred filename, or a caller-supplied default.
    """

    blockers = action_policy_validation_blockers(policy)
    if not isinstance(policy, Mapping) or policy.get("schema") != AUTONOMOUS_ACTION_POLICY_SCHEMA:
        return sorted(set(blockers + ["autonomous_policy_required"]))
    if not isinstance(state, Mapping):
        return sorted(set(blockers + ["autonomous_state_missing"]))
    if state.get("startup_binding_status") != "verified":
        blockers.append("autonomous_startup_binding_unverified")
    if state.get("validation_run_id") != policy.get("run_id"):
        blockers.append("autonomous_run_id_mismatch")
    if state.get("session_id") != policy.get("session_id"):
        blockers.append("autonomous_session_id_mismatch")
    if type(state.get("world_id")) is not int or state.get("world_id") != policy.get("world_id"):
        blockers.append("autonomous_world_id_mismatch")
    if policy.get("allowed_save_hashes") != [state.get("save_id_hash")]:
        blockers.append("autonomous_save_id_mismatch")
    return sorted(set(blockers))


def action_policy_hash(
    value: Any, *, allow_synthetic_source: bool = False
) -> str | None:
    if not action_policy_is_valid(value, allow_synthetic_source=allow_synthetic_source):
        return None
    document = policy_document(value, allow_synthetic_source=allow_synthetic_source)
    try:
        return canonical_hash(document)
    except (CanonicalHashError, TypeError, ValueError, OverflowError):
        return None


def project_action_policy(
    value: Any, *, allow_synthetic_source: bool = False
) -> Dict[str, Any]:
    document = (
        policy_document(value, allow_synthetic_source=allow_synthetic_source)
        if isinstance(value, Mapping)
        else {}
    )
    actions = document.get("allowed_actions")
    saves = document.get("allowed_save_hashes")
    valid = action_policy_is_valid(
        value, allow_synthetic_source=allow_synthetic_source
    )
    policy_hash = action_policy_hash(
        value, allow_synthetic_source=allow_synthetic_source
    )
    return {
        "valid": valid,
        "policy_hash": policy_hash if valid else None,
        "actions_enabled": (
            document.get("actions_enabled")
            if isinstance(document.get("actions_enabled"), bool)
            else False
        ),
        "suggestions_enabled": (
            document.get("suggestions_enabled")
            if isinstance(document.get("suggestions_enabled"), bool)
            else False
        ),
        "allowed_actions_count": len(actions) if isinstance(actions, list) else 0,
        "allowed_save_hash_count": len(saves) if isinstance(saves, list) else 0,
    }


__all__ = [
    "ACTION_POLICY_SCHEMA",
    "AUTONOMOUS_ACTION_POLICY_SCHEMA",
    "AUTONOMOUS_EXECUTION_MODE",
    "ALLOWED_ACTIONS",
    "MAX_COMMAND_TTL_SECONDS",
    "MAX_PENDING_COMMANDS",
    "MIN_COMMAND_TTL_SECONDS",
    "MIN_PENDING_COMMANDS",
    "action_policy_hash",
    "action_policy_is_valid",
    "action_policy_validation_blockers",
    "autonomous_identifier_is_valid",
    "autonomous_policy_state_blockers",
    "policy_document",
    "project_action_policy",
]
