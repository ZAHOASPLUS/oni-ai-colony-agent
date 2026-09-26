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
_SAVE_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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
    blockers.extend(
        f"policy_field_missing:{key}" for key in sorted(_REQUIRED_KEYS - keys)
    )
    blockers.extend(
        f"policy_field_unknown:{key}"
        for key in sorted(keys - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    )

    if document.get("schema") != ACTION_POLICY_SCHEMA:
        blockers.append("policy_schema_invalid")
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
    "ALLOWED_ACTIONS",
    "MAX_COMMAND_TTL_SECONDS",
    "MAX_PENDING_COMMANDS",
    "MIN_COMMAND_TTL_SECONDS",
    "MIN_PENDING_COMMANDS",
    "action_policy_hash",
    "action_policy_is_valid",
    "action_policy_validation_blockers",
    "policy_document",
    "project_action_policy",
]
